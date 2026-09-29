"""Tradução em lote dos capítulos importados: estimativa, fila de blocos e execução com novas tentativas.

Cada lote é dividido em requisições de N páginas (configurável). As falas de uma requisição são
calculadas na hora de enviar, só com o que ainda não tem tradução salva, e falas repetidas vão uma
vez só. Por isso retomar depois de fechar o app, reenviar só o que o modelo esqueceu ou repetir um
lote nunca paga duas vezes a mesma fala.

Erros temporários (limite de requisições, servidor, rede) são tentados de novo com espera crescente;
se persistirem, o lote pausa (e é retomado depois) em vez de queimar as requisições seguintes.
Erros definitivos (chave inválida, modelo inexistente, sem crédito) pausam o lote com a mensagem.
"""

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from platformdirs import user_log_dir
from PySide6.QtCore import QObject, Signal

from . import APP_NAME, credentials, llm_anthropic, llm_openai, openai_batch, pricing, translators
from .config import ENGINES, Config
from .db import Batch, BatchSummary, Character, Database, Memory, SourceLine, Term, TranslationKey, normalize
from .memory import estimate_summary_cost, summarize_chapters
from .pipeline import Pipeline
from .translators import Line, MalformedResponse, Result, TranslationError, Usage, llm_instructions, parse_response, parse_terms

# O lote sempre usa o modo texto: os capítulos importados já têm o texto lido pelo OCR local
TEXT_ENGINE = {"openai-vision": "openai-text", "claude-vision": "claude-text"}
LLM_ENGINES = ("openai-text", "claude-text")
# Limite de saída por resposta; o Claude usa max_tokens=16000 sem streaming (ver llm_anthropic)
_MAX_OUTPUT = {"gpt-4.1": 32_768, "gpt-4.1-mini": 32_768}
_DEFAULT_MAX_OUTPUT = 16_000
# Novas tentativas para erros temporários (segundos de espera antes de cada uma)
_RETRY_DELAYS = (2, 5, 15, 30, 60)
# Rodadas extras pedindo só as falas que o modelo deixou de devolver
_MISSING_ROUNDS = 2
_CONTEXT_LINES = 20
# Tudo o que aparece nos detalhes da janela de lote também vai para este arquivo (sobrevive a fechar o app)
LOG_FILE = Path(user_log_dir(APP_NAME, appauthor=False)) / "traducao-em-lote.log"
_LOG_LIMIT = 2 * 1024 * 1024
_REMOTE_LABELS = {
    "validating": "validando o arquivo", "in_progress": "processando", "finalizing": "finalizando", "cancelling": "cancelando",
    "completed": "terminou", "expired": "expirou (prazo de 24 h)", "cancelled": "cancelou o lote", "failed": "recusou o lote",
}


def max_output_tokens(model: str) -> int:
    if model in _MAX_OUTPUT:
        return _MAX_OUTPUT[model]
    if model.startswith("gpt-5"):
        return 128_000
    return _DEFAULT_MAX_OUTPUT


def batch_key(config: Config, work_id: int, source_lang: str) -> TranslationKey:
    engine = TEXT_ENGINE.get(config.engine, config.engine)
    key = Pipeline._db_key(replace(config, engine=engine, current_work=work_id, source_lang=source_lang))
    return key


def _output_tokens(text: str) -> int:
    """Tradução em português ~1,3x o texto de origem em tokens, mais o JSON de cada item."""
    return round(pricing.estimate_tokens(text) * 1.3) + 12


# --- estimativa ------------------------------------------------------------------------


@dataclass
class Estimate:
    pages: int
    lines: int  # falas nas páginas escolhidas
    new_lines: int  # sem tradução ainda (repetidas contam uma vez)
    requests: int
    input_tokens: int
    output_tokens: int
    cost: float | None  # None: modelo sem preço conhecido; 0: motor gratuito
    max_pages_per_block: int  # teto seguro para o limite de saída do modelo
    summary_cost: float | None = 0.0  # resumos da memória (um por capítulo, modelo barato)


def estimate(db: Database, config: Config, work_id: int, source_lang: str, chapter_ids: list[int], pages_per_block: int) -> Estimate:
    key = batch_key(config, work_id, source_lang)
    by_chapter = db.chapter_lines(chapter_ids)
    lines = [(page, text) for chapter in chapter_ids for page, text in by_chapter.get(chapter, [])]
    saved = db.find_translations(key, [t for _p, t in lines], fuzzy=False)
    page_ids = db.chapter_pages(chapter_ids)

    # Falas novas por página, contando cada texto repetido só na primeira vez que aparece
    seen = set(saved)
    new_by_page: dict[int, list[str]] = {}
    for page, text in lines:
        norm = normalize(text)
        if norm and norm not in seen:
            seen.add(norm)
            new_by_page.setdefault(page, []).append(text)
    new_lines = sum(len(v) for v in new_by_page.values())

    output_by_page = {page: sum(_output_tokens(t) for t in texts) for page, texts in new_by_page.items()}
    blocks = [page_ids[i : i + pages_per_block] for i in range(0, len(page_ids), pages_per_block)]
    requests = sum(1 for block in blocks if any(p in new_by_page for p in block))

    characters = db.characters(work_id)
    memory = db.memory(work_id)
    # A memória cresce durante o lote: estima com ~1.500 tokens se ainda estiver vazia
    memory_tokens = 0 if not memory.empty else 1500
    fixed = pricing.estimate_tokens(llm_instructions(source_lang, key.target, False, characters, memory)) + memory_tokens + 25 * _CONTEXT_LINES
    input_tokens = requests * fixed + sum(pricing.estimate_tokens(t) + 10 for texts in new_by_page.values() for t in texts)
    output_tokens = sum(output_by_page.values())

    # Resumo de cada capítulo com falas novas (modelo barato; entrada ≈ a tradução do capítulo)
    chapter_of_page = {page: chapter for chapter in chapter_ids for page, _t in by_chapter.get(chapter, [])}
    summary_tokens: dict[int, int] = {}
    for page, tokens in output_by_page.items():
        summary_tokens[chapter_of_page[page]] = summary_tokens.get(chapter_of_page[page], 0) + tokens
    summary_cost = estimate_summary_cost(config, key.engine, list(summary_tokens.values())) if key.engine in LLM_ENGINES else 0.0

    if key.engine in LLM_ENGINES:
        cost = pricing.cost(key.model, input_tokens, output_tokens) if requests else 0.0
        per_page = max(output_by_page.values(), default=0) or 1
        # Metade do limite de saída como margem (respostas maiores tendem a pular falas)
        max_pages = max(1, min(100, math.floor(max_output_tokens(key.model) * 0.5 / per_page)))
    else:
        cost, max_pages = 0.0, 100
    return Estimate(len(page_ids), len(lines), new_lines, requests, input_tokens, output_tokens, cost, max_pages, summary_cost)


# --- execução ----------------------------------------------------------------------------


@dataclass
class BatchProgress:
    batch_id: int
    done: int
    total: int
    failed: int
    cost: float
    message: str


@dataclass
class BatchLogEntry:
    time: datetime
    level: str  # "info", "ok", "aviso" ou "erro"
    message: str


@dataclass
class BatchOutcome:
    batch_id: int
    state: str  # "concluido", "pausado" ou "interrompido" (app fechando; retoma ao abrir)
    summary: BatchSummary
    error: str | None


class _Pause(Exception):
    """Interrompe o lote: erro definitivo ou temporário que persistiu depois das novas tentativas."""


class BatchRunner(QObject):
    progress = Signal(object)  # BatchProgress
    finished = Signal(object)  # BatchOutcome
    log = Signal(object)  # BatchLogEntry: o passo a passo, para os detalhes da janela de lote

    def __init__(self, db: Database, pipeline: Pipeline, parent: QObject | None = None):
        super().__init__(parent)
        self._db = db
        self._pipeline = pipeline
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause_requested = False
        # Último estado visto de cada lote da Batch API: o log só registra quando algo muda
        self._remote_seen: dict[int, tuple] = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: Config) -> bool:
        """Executa todos os lotes ativos, em ordem. False se já estiver rodando."""
        if self.running:
            return False
        self._stop.clear()
        self._pause_requested = False
        self._thread = threading.Thread(target=self._run, args=(replace(config),), name="traducao-em-lote", daemon=True)
        self._thread.start()
        return True

    def pause(self) -> None:
        """Pausa pelo usuário: o lote fica "pausado" e só volta pelo menu (não retoma sozinho ao abrir o app)."""
        self._pause_requested = True
        self._stop.set()

    def interrupt(self) -> None:
        """App fechando: o lote continua "ativo" e é retomado automaticamente na próxima vez."""
        self._stop.set()

    def wait(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _log(self, level: str, message: str) -> None:
        entry = BatchLogEntry(datetime.now(), level, message)
        self.log.emit(entry)
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > _LOG_LIMIT:
                LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".1"))
            with LOG_FILE.open("a", encoding="utf-8") as file:
                file.write(f"{entry.time:%Y-%m-%d %H:%M:%S} [{level}] {message}\n")
        except OSError:
            pass  # o log em arquivo é um extra; a janela continua recebendo

    def _log_start(self, batch: Batch, key: TranslationKey, total: int, pending: int) -> None:
        work = self._db.work(batch.work_id)
        engine = key.model if key.engine in LLM_ENGINES else ENGINES.get(key.engine, key.engine)
        mode = " · Batch API da OpenAI" if batch.mode == "batch" else ""
        done = total - pending
        self._log(
            "info",
            f"Lote {batch.id} — “{work.name if work else '?'}” · {engine}{mode} · {total} bloco(s) de até {batch.pages_per_block} página(s)"
            + (f" · retomando: {done} já feito(s), {pending} a fazer" if done else ""),
        )

    def _log_outcome(self, batch: Batch, state: str, error: str | None) -> None:
        s = self._db.batch_summary(batch.id)
        cost = pricing.format_cost(s.cost) if s.cost else "US$ 0"
        if state == "concluido":
            tokens = (
                f" · {pricing.thousands(s.input_tokens)} tokens de entrada ({pricing.thousands(s.cached_tokens)} do cache),"
                f" {pricing.thousands(s.output_tokens)} de saída"
                if s.input_tokens
                else ""
            )
            self._log("ok", f"Lote {batch.id} concluído: {s.done} de {s.requests} bloco(s){tokens} · custo real {cost}")
            if s.failed:
                self._log("aviso", f"{s.failed} bloco(s) ficaram com falas sem tradução (em geral reticências e onomatopeias que o modelo não traduz).")
        elif state == "pausado":
            if error:
                self._log("erro", f"Lote {batch.id} pausado: {error}")
            else:
                self._log("info", f"Lote {batch.id} pausado a pedido ({s.done} de {s.requests} bloco(s) prontos, {cost}).")
            self._log("info", "Para continuar: “Retomar tradução em lote” no menu da bandeja.")
        else:
            self._log("info", f"Lote {batch.id} interrompido porque o app está fechando; continua sozinho ao abrir de novo.")

    def _run(self, config: Config) -> None:
        for batch in self._db.batches(("ativo",)):
            if self._stop.is_set():
                break
            if batch.mode == "batch":
                self._step_remote(batch, config)
            else:
                self._run_batch(batch, config)

    def _run_batch(self, batch: Batch, config: Config) -> None:
        key = TranslationKey(batch.work_id, batch.source_lang, batch.target_lang, batch.engine, batch.model)
        characters = self._db.characters(batch.work_id)
        all_requests = self._db.batch_requests(batch.id, ("pendente", "concluida", "falhou"))
        pending = [r for r in all_requests if r.state == "pendente"]
        context: deque[str] = deque(maxlen=_CONTEXT_LINES)
        if pending:
            # Retomando: as falas do bloco anterior dão continuidade de cena
            previous = [r for r in all_requests if r.order < pending[0].order]
            if previous:
                context.extend(line.text for line in self._db.request_lines(previous[-1].id))
        self._log_start(batch, key, len(all_requests), len(pending))

        state, error = "concluido", None
        for request in pending:
            if self._stop.is_set():
                state = "pausado" if self._pause_requested else "interrompido"
                break
            try:
                self._execute(batch, key, request.id, request.order, characters, context, config, len(all_requests))
            except _Pause as exc:
                if self._stop.is_set():
                    # Parado durante uma espera de nova tentativa: pausa do usuário ou app fechando
                    state = "pausado" if self._pause_requested else "interrompido"
                else:
                    state, error = "pausado", str(exc)
                break

        if state == "pausado":
            self._db.set_batch_state(batch.id, "pausado", error)
        elif state == "concluido":
            self._db.set_batch_state(batch.id, "concluido")
        self._log_outcome(batch, state, error)
        self.finished.emit(BatchOutcome(batch.id, state, self._db.batch_summary(batch.id), error))

    def _emit(self, batch: Batch, total: int, message: str) -> None:
        summary = self._db.batch_summary(batch.id)
        self.progress.emit(BatchProgress(batch.id, summary.done + summary.failed, total, summary.failed, summary.cost, message))

    def _execute(
        self,
        batch: Batch,
        key: TranslationKey,
        request_id: int,
        order: int,
        characters: list[Character],
        context: deque,
        config: Config,
        total: int,
    ) -> None:
        source_lines = self._db.request_lines(request_id)
        memory = self._db.memory(batch.work_id)  # foto: não muda durante o bloco
        usage = Usage()
        cost = 0.0
        missing: list = []
        where = _where(source_lines)
        for _round in range(1 + _MISSING_ROUNDS):
            missing = self._missing(key, source_lines)
            if not missing:
                if _round == 0:
                    self._log("info", f"Bloco {order} de {total} ({where}): nada a enviar, todas as falas já tinham tradução salva")
                break
            if _round == 0:
                self._log("info", f"Bloco {order} de {total} ({where}): enviando {len(missing)} fala(s)")
            else:
                self._log(
                    "aviso",
                    f"Bloco {order}: {len(missing)} fala(s) ficaram sem resposta; pedindo só elas de novo"
                    f" (tentativa {_round + 1} de {1 + _MISSING_ROUNDS})",
                )
            self._emit(batch, total, f"Bloco {order} de {total}: {len(missing)} fala(s)")
            lines = [Line(n, line.text, f"{line.chapter}, p. {line.page}") for n, line in enumerate(missing, start=1)]
            started = time.monotonic()
            try:
                results, call_usage, terms = self._call(key, lines, list(context), characters, memory, config, order)
            except MalformedResponse as exc:
                self._log("aviso", f"Bloco {order}: resposta fora do formato esperado ({str(exc).rstrip('.')}); pedindo de novo")
                continue  # resposta fora do formato: nova rodada com o que falta
            except TranslationError as exc:
                self._db.record_request(request_id, "pendente", _usage_tuple(usage), cost, str(exc))
                raise _Pause(str(exc)) from exc
            usage = usage + call_usage
            call_cost = pricing.cost(key.model, call_usage.input_tokens, call_usage.output_tokens, call_usage.cached_tokens) or 0.0
            cost += call_cost
            untranslated = sum(1 for r in results if not r.translation)
            details = f" · {untranslated} sem tradução (fica o original)" if untranslated else ""
            if key.engine in LLM_ENGINES:
                details += (
                    f" · {pricing.thousands(call_usage.input_tokens)} tokens de entrada"
                    f" ({pricing.thousands(call_usage.cached_tokens)} do cache), {pricing.thousands(call_usage.output_tokens)} de saída"
                    f" · {pricing.format_cost(call_cost)}"
                )
            self._log("ok", f"Bloco {order}: {len(results)} de {len(lines)} fala(s) traduzidas em {_seconds(time.monotonic() - started)}{details}")
            if terms:
                sample = ", ".join(f"{t.original} → {t.translation}" for t in terms[:4]) + ("…" if len(terms) > 4 else "")
                self._log("info", f"Glossário: {len(terms)} termo(s) anotados pelo modelo ({sample})")
            # Fala devolvida sem tradução (reticências, onomatopeias): grava o próprio original como "não precisa
            # traduzir". Sem isso ela era reenviada em toda rodada e em todo "Retomar", sempre paga e sempre vazia.
            self._db.save_translations(key, [(lines[r.id - 1].text, r.translation or lines[r.id - 1].text) for r in results])
            self._db.add_terms(batch.work_id, terms)

        context.extend(line.text for line in source_lines)
        if missing:
            sample = "; ".join(line.text for line in missing[:3]) + ("…" if len(missing) > 3 else "")
            self._log("aviso", f"Bloco {order}: {len(missing)} fala(s) ficaram sem tradução depois de {1 + _MISSING_ROUNDS} tentativas: {sample}")
            self._db.record_request(
                request_id, "falhou", _usage_tuple(usage), cost, f"{len(missing)} fala(s) ficaram sem tradução depois de {1 + _MISSING_ROUNDS} tentativas"
            )
        else:
            self._db.record_request(request_id, "concluida", _usage_tuple(usage), cost)
        self._update_memory(key, config, request_id)
        self._emit(batch, total, f"Bloco {order} de {total} concluído")

    def _update_memory(self, key: TranslationKey, config: Config, request_id: int) -> None:
        """Capítulos que ficaram completos: resumo da história e nova foto do glossário (custo somado ao lote)."""
        if key.engine not in LLM_ENGINES:
            return
        result = summarize_chapters(self._db, config, key)
        if result.chapters:
            cost = f" · {pricing.format_cost(result.cost)}" if result.cost else ""
            self._log("ok", f"Capítulo(s) completo(s): {', '.join(result.chapters)} · memória da obra atualizada (resumo e glossário){cost}")
        if result.error:
            self._log("aviso", f"Não foi possível resumir o capítulo {result.error}. Tenta de novo quando o próximo capítulo terminar.")
        if result.cost or result.usage.input_tokens:
            self._db.add_request_cost(request_id, _usage_tuple(result.usage), result.cost)

    def _missing(self, key: TranslationKey, source_lines: list[SourceLine]) -> list[SourceLine]:
        """Uma fala por texto (as repetidas usam a mesma tradução), só as que ainda não têm tradução salva."""
        saved = self._db.find_translations(key, [line.text for line in source_lines], fuzzy=False)
        unique: dict[str, SourceLine] = {}
        for line in source_lines:
            norm = normalize(line.text)
            if norm and norm not in saved and norm not in unique:
                unique[norm] = line
        return list(unique.values())

    # --- Batch API da OpenAI -------------------------------------------------------------

    def _step_remote(self, batch: Batch, config: Config) -> None:
        """Uma passada num lote da Batch API: envia (se ainda não enviado) ou confere o andamento.

        O app chama de novo a cada minuto enquanto houver lote esperando a OpenAI.
        """
        api_key = credentials.get_key(credentials.OPENAI)
        if not api_key:
            self._finish(batch, "pausado", "Chave da API da OpenAI não configurada.")
            return
        key = TranslationKey(batch.work_id, batch.source_lang, batch.target_lang, batch.engine, batch.model)
        try:
            if batch.remote_id is None:
                if batch.pause_requested:
                    self._db.request_pause(batch.id, False)
                    self._finish(batch, "pausado", None)
                elif self._run_sync_part(batch, key, config):
                    self._submit_remote(batch, key, api_key, batch.round + 1, config)
            else:
                self._poll_remote(batch, key, api_key, config)
        except TranslationError as exc:
            if exc.retryable:
                # Sem conexão agora: o lote segue ativo e a próxima passada tenta de novo
                self._log("aviso", f"Lote {batch.id}: {exc} Tentando de novo em 1 minuto.")
                self.progress.emit(BatchProgress(batch.id, 0, 0, 0, self._db.batch_summary(batch.id).cost, f"{exc} Tentando de novo em 1 minuto."))
            else:
                self._finish(batch, "pausado", str(exc))

    def _run_sync_part(self, batch: Batch, key: TranslationKey, config: Config) -> bool:
        """Modo híbrido: traduz na hora os blocos marcados como síncronos (e resume os capítulos deles), para o
        resto ir à OpenAI já com a memória. False se o lote pausou no meio."""
        all_requests = self._db.batch_requests(batch.id, ("pendente", "concluida", "falhou"))
        sync = [r for r in all_requests if r.sync and r.state == "pendente"]
        if not sync:
            return True
        self._log_start(batch, key, len(all_requests), len(all_requests) - sum(1 for r in all_requests if r.state != "pendente"))
        self._log("info", f"Modo híbrido: traduzindo agora {len(sync)} bloco(s) do 1º capítulo, para montar a memória antes de enviar o resto")
        characters = self._db.characters(batch.work_id)
        context: deque[str] = deque(maxlen=_CONTEXT_LINES)
        for request in sync:
            if self._stop.is_set():
                return False
            try:
                self._execute(batch, key, request.id, request.order, characters, context, config, len(all_requests))
            except _Pause as exc:
                self._finish(batch, "pausado", None if self._stop.is_set() else str(exc))
                return False
        return True

    def _finish(self, batch: Batch, state: str, error: str | None) -> None:
        self._db.set_batch_state(batch.id, state, error)
        self._remote_seen.pop(batch.id, None)
        self._log_outcome(batch, state, error)
        self.finished.emit(BatchOutcome(batch.id, state, self._db.batch_summary(batch.id), error))

    def _submit_remote(self, batch: Batch, key: TranslationKey, api_key: str, round: int, config: Config) -> None:
        characters = self._db.characters(batch.work_id)
        memory = self._db.memory(batch.work_id)  # mesma foto em todos os pedidos do envio (cache de prompt)
        all_requests = self._db.batch_requests(batch.id, ("pendente", "concluida", "falhou"))
        payload: dict[str, dict] = {}
        for request in self._db.batch_requests(batch.id, ("pendente",)):
            missing = self._missing(key, self._db.request_lines(request.id))
            if not missing:
                self._db.record_request(request.id, "concluida", (0, 0, 0), 0.0)
                continue
            if round > 1 + _MISSING_ROUNDS:
                self._log("aviso", f"Bloco {request.order}: {len(missing)} fala(s) ficaram sem tradução depois de {round - 1} envios")
                self._db.record_request(
                    request.id, "falhou", (0, 0, 0), 0.0, f"{len(missing)} fala(s) ficaram sem tradução depois de {round - 1} envios"
                )
                continue
            # Contexto: as falas do bloco anterior (texto original, já conhecido antes de qualquer tradução)
            previous = [r for r in all_requests if r.order < request.order]
            context = [line.text for line in self._db.request_lines(previous[-1].id)][-_CONTEXT_LINES:] if previous else []
            lines = [Line(n, line.text, f"{line.chapter}, p. {line.page}") for n, line in enumerate(missing, start=1)]
            self._db.set_sent(request.id, [line.text for line in missing])
            payload[str(request.id)] = llm_openai.text_body(
                lines, context, key.source, key.target, key.model, characters, memory, batch.work_id
            )

        if not payload:
            self._db.set_remote(batch.id, None, None, round - 1)
            self._finish(batch, "concluido", None)
            return
        if round == 1:
            self._log_start(batch, key, len(all_requests), len(payload))
        remote_id = openai_batch.submit(api_key, payload)
        self._db.set_remote(batch.id, remote_id, "validating", round)
        extra = f" (envio {round}: só as falas que faltaram)" if round > 1 else ""
        self._log(
            "info",
            f"{len(payload)} pedido(s) enviados à Batch API da OpenAI{extra} (id {remote_id}). "
            "A OpenAI processa em segundo plano (em geral minutos, até 24 h); o app confere a cada minuto e pode ser fechado.",
        )
        self.progress.emit(
            BatchProgress(batch.id, 0, len(payload), 0, self._db.batch_summary(batch.id).cost,
                          f"{len(payload)} pedido(s) enviados à Batch API da OpenAI{extra}. Aguardando a OpenAI processar…")
        )

    def _poll_remote(self, batch: Batch, key: TranslationKey, api_key: str, config: Config) -> None:
        remote = openai_batch.status(api_key, batch.remote_id)
        self._db.set_remote(batch.id, batch.remote_id, remote.status)
        if batch.pause_requested and remote.status in ("validating", "in_progress", "finalizing"):
            # Cancelar devolve o que a OpenAI já fez; o resto fica pendente para quando retomar
            openai_batch.cancel(api_key, batch.remote_id)
            remote.status = "cancelling"
            self._log("info", "Pausa pedida: cancelando o lote na OpenAI (o que já voltou é guardado; o resto fica para quando retomar)")
        seen = (batch.remote_id, remote.status, remote.completed, remote.failed)
        if self._remote_seen.get(batch.id) != seen:
            self._remote_seen[batch.id] = seen
            label = _REMOTE_LABELS.get(remote.status, remote.status)
            failed = f", {remote.failed} com erro" if remote.failed else ""
            self._log("info", f"OpenAI {label}: {remote.completed} de {remote.total} pedido(s) prontos{failed}")
        if remote.status in openai_batch.WAITING:
            self.progress.emit(
                BatchProgress(batch.id, remote.completed + remote.failed, remote.total, remote.failed,
                              self._db.batch_summary(batch.id).cost, f"OpenAI {_REMOTE_LABELS[remote.status]}: {remote.completed} de {remote.total} pedido(s) prontos")
            )
            return
        if remote.status == "failed":
            self._db.set_remote(batch.id, None, None)
            self._finish(batch, "pausado", "A OpenAI recusou o lote: " + ("; ".join(remote.errors) or "sem detalhes"))
            return

        # Concluído, expirado ou cancelado: grava o que voltou
        received, errors, received_cost = 0, [], 0.0
        for item in openai_batch.download(api_key, remote.output_file_id):
            request_id = int(item["custom_id"])
            response = item.get("response") or {}
            if response.get("status_code") != 200:
                error = (response.get("body") or {}).get("error", {}).get("message", "erro desconhecido")
                errors.append(error)
                self._db.record_request(request_id, "pendente", (0, 0, 0), 0.0, error)
                continue
            text, usage = llm_openai.parse_body(response.get("body") or {})
            sent = self._db.sent(request_id)
            try:
                results = parse_response(text, [Line(n, t) for n, t in enumerate(sent, start=1)])
            except MalformedResponse:
                results = []  # as falas continuam faltando e vão no próximo envio
            self._db.save_translations(key, [(sent[r.id - 1], r.translation or sent[r.id - 1]) for r in results])
            self._db.add_terms(batch.work_id, parse_terms(text))
            cost = pricing.cost(key.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, openai_batch.DISCOUNT) or 0.0
            self._db.record_request(request_id, "pendente", _usage_tuple(usage), cost)
            received += 1
            received_cost += cost
        for item in openai_batch.download(api_key, remote.error_file_id):
            error = (item.get("error") or {}).get("message", "erro desconhecido")
            errors.append(error)
            self._db.record_request(int(item["custom_id"]), "pendente", (0, 0, 0), 0.0, error)
        self._db.set_remote(batch.id, None, None)
        self._remote_seen.pop(batch.id, None)
        self._log("ok", f"Resultados recebidos da OpenAI: {received} pedido(s) com resposta · {pricing.format_cost(received_cost)}")
        for error in dict.fromkeys(errors):  # cada mensagem uma vez
            self._log("erro", f"{errors.count(error)} pedido(s) voltaram com erro: {error} (vão de novo no próximo envio)")

        # Capítulos que ficaram completos: resumo e nova foto do glossário (para os próximos lotes e a leitura)
        first = self._db.batch_requests(batch.id, ("pendente", "concluida", "falhou"))
        self._update_memory(key, config, first[0].id)
        if batch.pause_requested:
            self._db.request_pause(batch.id, False)
            self._finish(batch, "pausado", None)
            return
        # Marca o que ficou completo e reenvia só as falas que faltaram (até 2 rodadas extras)
        self._submit_remote(batch, key, api_key, batch.round + 1, config)

    def _call(
        self,
        key: TranslationKey,
        lines: list[Line],
        context: list[str],
        characters: list[Character],
        memory: Memory,
        config: Config,
        order: int,
    ) -> tuple[list[Result], Usage, list[Term]]:
        """Uma chamada ao motor, com novas tentativas em erros temporários."""
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                if key.engine == "openai-text":
                    return llm_openai.translate_text(
                        lines, context, key.source, key.target, key.model, credentials.get_key(credentials.OPENAI), characters, memory,
                        key.work_id,
                    )
                if key.engine == "claude-text":
                    return llm_anthropic.translate_text(
                        lines, context, key.source, key.target, key.model, credentials.get_key(credentials.ANTHROPIC), characters, memory
                    )
                if key.engine == "local":
                    local = replace(config, source_lang=key.source, target_lang=key.target)
                    return self._pipeline.translate_offline(lines, local), Usage(), []
                return translators.translate_google(lines, key.source, key.target), Usage(), []
            except TranslationError as exc:
                if not exc.retryable or delay is None or self._stop.is_set():
                    raise
                self._log("aviso", f"Bloco {order}: erro temporário: {str(exc).rstrip('.')}. Nova tentativa em {delay} s ({attempt + 1} de {len(_RETRY_DELAYS)})")
                # Espera interrompível: pausar ou fechar o app não fica preso numa espera de 60 s
                if self._stop.wait(delay):
                    raise
        raise AssertionError("inalcançável")


def _seconds(value: float) -> str:
    return f"{value:.1f} s".replace(".", ",")


def _where(lines: list[SourceLine]) -> str:
    """ "Cap 3, p. 1 a p. 20" — onde estão as falas do bloco."""
    if not lines:
        return "sem falas"
    first, last = lines[0], lines[-1]
    if (first.chapter, first.page) == (last.chapter, last.page):
        return f"{first.chapter}, p. {first.page}"
    if first.chapter == last.chapter:
        return f"{first.chapter}, p. {first.page} a {last.page}"
    return f"{first.chapter}, p. {first.page} a {last.chapter}, p. {last.page}"


def _usage_tuple(usage: Usage) -> tuple[int, int, int]:
    return usage.input_tokens, usage.cached_tokens, usage.output_tokens
