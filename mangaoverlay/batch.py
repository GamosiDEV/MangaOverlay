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
from collections import deque
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, Signal

from . import credentials, llm_anthropic, llm_openai, pricing, translators
from .config import Config
from .db import Batch, BatchSummary, Character, Database, TranslationKey, normalize
from .pipeline import Pipeline
from .translators import Line, MalformedResponse, Result, TranslationError, Usage, llm_instructions

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
    fixed = pricing.estimate_tokens(llm_instructions(source_lang, key.target, False, characters)) + 25 * _CONTEXT_LINES
    input_tokens = requests * fixed + sum(pricing.estimate_tokens(t) + 10 for texts in new_by_page.values() for t in texts)
    output_tokens = sum(output_by_page.values())

    if key.engine in LLM_ENGINES:
        cost = pricing.cost(key.model, input_tokens, output_tokens) if requests else 0.0
        per_page = max(output_by_page.values(), default=0) or 1
        # Metade do limite de saída como margem (respostas maiores tendem a pular falas)
        max_pages = max(1, min(100, math.floor(max_output_tokens(key.model) * 0.5 / per_page)))
    else:
        cost, max_pages = 0.0, 100
    return Estimate(len(page_ids), len(lines), new_lines, requests, input_tokens, output_tokens, cost, max_pages)


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

    def __init__(self, db: Database, pipeline: Pipeline, parent: QObject | None = None):
        super().__init__(parent)
        self._db = db
        self._pipeline = pipeline
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause_requested = False

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

    def _run(self, config: Config) -> None:
        for batch in self._db.batches(("ativo",)):
            if self._stop.is_set():
                break
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
        usage = Usage()
        cost = 0.0
        missing: list = []
        for _round in range(1 + _MISSING_ROUNDS):
            saved = self._db.find_translations(key, [line.text for line in source_lines], fuzzy=False)
            # Uma fala por texto (as repetidas usam a mesma tradução), só as que ainda não têm tradução
            unique: dict[str, object] = {}
            for line in source_lines:
                norm = normalize(line.text)
                if norm and norm not in saved and norm not in unique:
                    unique[norm] = line
            missing = list(unique.values())
            if not missing:
                break
            self._emit(batch, total, f"Bloco {order} de {total}: {len(missing)} fala(s)")
            lines = [Line(n, line.text, f"{line.chapter}, p. {line.page}") for n, line in enumerate(missing, start=1)]
            try:
                results, call_usage = self._call(key, lines, list(context), characters, config)
            except MalformedResponse:
                continue  # resposta fora do formato: nova rodada com o que falta
            except TranslationError as exc:
                self._db.record_request(request_id, "pendente", _usage_tuple(usage), cost, str(exc))
                raise _Pause(str(exc)) from exc
            usage = usage + call_usage
            cost += pricing.cost(key.model, call_usage.input_tokens, call_usage.output_tokens, call_usage.cached_tokens) or 0.0
            self._db.save_translations(key, [(lines[r.id - 1].text, r.translation) for r in results if r.translation])

        context.extend(line.text for line in source_lines)
        if missing:
            self._db.record_request(
                request_id, "falhou", _usage_tuple(usage), cost, f"{len(missing)} fala(s) ficaram sem tradução depois de {1 + _MISSING_ROUNDS} tentativas"
            )
        else:
            self._db.record_request(request_id, "concluida", _usage_tuple(usage), cost)
        self._emit(batch, total, f"Bloco {order} de {total} concluído")

    def _call(
        self, key: TranslationKey, lines: list[Line], context: list[str], characters: list[Character], config: Config
    ) -> tuple[list[Result], Usage]:
        """Uma chamada ao motor, com novas tentativas em erros temporários."""
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                if key.engine == "openai-text":
                    return llm_openai.translate_text(
                        lines, context, key.source, key.target, key.model, credentials.get_key(credentials.OPENAI), characters
                    )
                if key.engine == "claude-text":
                    return llm_anthropic.translate_text(
                        lines, context, key.source, key.target, key.model, credentials.get_key(credentials.ANTHROPIC), characters
                    )
                if key.engine == "local":
                    local = replace(config, source_lang=key.source, target_lang=key.target)
                    return self._pipeline.translate_offline(lines, local), Usage()
                return translators.translate_google(lines, key.source, key.target), Usage()
            except TranslationError as exc:
                if not exc.retryable or delay is None or self._stop.is_set():
                    raise
                # Espera interrompível: pausar ou fechar o app não fica preso numa espera de 60 s
                if self._stop.wait(delay):
                    raise
        raise AssertionError("inalcançável")


def _usage_tuple(usage: Usage) -> tuple[int, int, int]:
    return usage.input_tokens, usage.cached_tokens, usage.output_tokens
