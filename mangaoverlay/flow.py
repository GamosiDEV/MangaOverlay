"""Fluxo completo: importar os capítulos → conferir o custo → traduzir → gerar o resultado, uma etapa atrás da outra.

O fluxo não faz o trabalho: ele usa o importador, a tradução em lote e a geração que o app já tem, e só decide quando
começar cada um. Tudo o que ele sabe fica no banco (tabela `fluxos`): a etapa atual e as escolhas feitas no assistente.
Por isso fechar o app no meio (por exemplo, esperando a Batch API, que leva até 24 h) não perde nada: ao abrir de novo,
`tick()` olha o banco e continua de onde parou.

Custo: capítulos recém-importados só têm custo conhecido depois da leitura. Se a estimativa passar do limite escolhido,
o fluxo pausa e pergunta; abaixo dele, segue sozinho.
"""

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

from platformdirs import user_log_dir
from PySide6.QtCore import QObject, Signal

from . import APP_NAME, pricing
from .batch import LLM_ENGINES, BatchOptions, create_batch, estimate_plan
from .config import ENGINES, Config
from .db import Database, FlowRow

# O passo a passo de todos os fluxos (cada linha marcada com o número do fluxo), como o log da tradução em lote
LOG_FILE = Path(user_log_dir(APP_NAME, appauthor=False)) / "fluxo-completo.log"
_LOG_LIMIT = 2 * 1024 * 1024
_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] \[fluxo (\d+)\] (.*)$")

STEPS = ("importar", "custo", "traduzir", "gerar", "fim")
STEP_LABELS = {
    "importar": "Ler os capítulos (detecção e OCR)",
    "custo": "Conferir o custo",
    "traduzir": "Traduzir",
    "gerar": "Gerar o resultado",
    "fim": "Concluído",
}


@dataclass
class FlowLogEntry:
    time: datetime
    level: str  # "info", "ok", "aviso" ou "erro"
    flow_id: int
    message: str


def read_log(flow_id: int, path: Path | None = None) -> list[FlowLogEntry]:
    """As linhas do log de um fluxo (do arquivo anterior, se o atual já foi rodado, e do atual)."""
    path = path or LOG_FILE
    entries = []
    for file in (path.with_name(path.name + ".1"), path):
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            match = _LOG_LINE.match(line)
            if match and int(match.group(3)) == flow_id:
                entries.append(FlowLogEntry(datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"), match.group(2), flow_id, match.group(4)))
    return entries


@dataclass
class FlowOptions:
    """As escolhas do assistente (vão em JSON para o banco)."""

    chapter_ids: list[int]
    engine: str
    openai_model: str
    claude_model: str
    pages_per_block: int = 20
    mode: str = "normal"
    memory_chapters: int = 0
    memory_block: int = 0
    parts: int = 1
    survey_names: bool = False
    cost_limit: float | None = None  # US$; None: nunca pergunta
    cost_confirmed: bool = False  # o usuário aceitou passar do limite
    generate: bool = False
    fmt: str = "cbz"
    folder: str = ""
    inpaint: bool = False
    generated: list[str] = field(default_factory=list)  # arquivos gravados (para a mensagem final)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "FlowOptions":
        data = json.loads(text)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def config(self, base: Config) -> Config:
        """A configuração do app com o motor e os modelos escolhidos para este fluxo."""
        return replace(base, engine=self.engine, openai_model=self.openai_model, claude_model=self.claude_model)

    def batch_options(self) -> BatchOptions:
        return BatchOptions(
            self.chapter_ids, self.pages_per_block, self.mode, self.memory_chapters, self.memory_block, self.parts, self.survey_names
        )


class FlowRunner(QObject):
    """Avança os fluxos ativos. `tick()` é chamado pelos sinais do importador, do lote e da geração e por um
    temporizador; cada chamada leva cada fluxo o mais longe que der agora."""

    changed = Signal(int)  # id do fluxo que mudou (a janela atualiza)
    log = Signal(object)  # FlowLogEntry
    attention = Signal(int)  # o fluxo terminou ou pausou e precisa ser visto (o app abre a janela)

    def __init__(
        self,
        db: Database,
        config: Callable[[], Config],
        importer,
        batch_runner,
        generator,
        notify: Callable[[str, bool], None] = lambda _message, _warning: None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._db = db
        self._config = config
        self._importer = importer
        self._batch_runner = batch_runner
        self._generator = generator
        self._notify = notify
        self._generating: int | None = None  # fluxo cuja geração está rodando

    # --- log ------------------------------------------------------------------------------

    def _log(self, flow_id: int, level: str, message: str) -> None:
        entry = FlowLogEntry(datetime.now(), level, flow_id, message)
        self.log.emit(entry)
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > _LOG_LIMIT:
                LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".1"))
            with LOG_FILE.open("a", encoding="utf-8") as file:
                file.write(f"{entry.time:%Y-%m-%d %H:%M:%S} [{level}] [fluxo {flow_id}] {message}\n")
        except OSError:
            pass  # o arquivo é um extra; a janela continua recebendo

    # --- sinais dos executores (chegam sempre na thread principal: o app liga com conexão enfileirada) -----------

    def on_import_finished(self, summary) -> None:
        for flow in self._db.flows(("ativo",)):
            if flow.step == "importar":
                errors = f", {summary.errors} página(s) com erro" if summary.errors else ""
                level = "aviso" if summary.errors else "ok"
                self._log(flow.id, level, f"Leitura: {summary.pages} página(s) lida(s), {summary.texts} falas{errors}")
        self.tick()

    def on_batch_finished(self, _outcome) -> None:
        self.tick()

    def on_batch_log(self, entry) -> None:
        """O passo a passo da tradução em lote também vai para o log do fluxo que está traduzindo."""
        for flow in self._db.flows(("ativo", "pausado")):
            if flow.step == "traduzir" and flow.batch_id is not None:
                self._log(flow.id, entry.level, f"Lote: {entry.message}")

    # --- ações do usuário ------------------------------------------------------------

    def start(self, work_id: int, options: FlowOptions) -> int:
        flow_id = self._db.create_flow(work_id, options.to_json())
        work = self._db.work(work_id)
        engine = ENGINES.get(options.engine, options.engine)
        model = {"openai-text": options.openai_model, "claude-text": options.claude_model}.get(options.engine)
        details = [f"{len(options.chapter_ids)} capítulo(s)", engine + (f" · {model}" if model else "")]
        details.append("Batch API" if options.mode == "batch" else "envio normal")
        if options.mode == "batch" and options.memory_chapters:
            details.append(f"{options.memory_chapters} capítulo(s) de memória")
        if options.mode == "batch" and options.parts > 1:
            details.append(f"{options.parts} partes")
        if options.survey_names:
            details.append("nomes automáticos")
        if options.cost_limit is not None and options.engine in LLM_ENGINES:
            details.append(f"limite de custo US$ {options.cost_limit:.2f}".replace(".", ","))
        if options.generate:
            details.append(f"gerar {'pasta de imagens' if options.fmt == 'pasta' else 'CBZ'} em {options.folder}"
                           + (", reconstruindo o desenho" if options.inpaint else ""))
        self._log(flow_id, "info", f"Fluxo iniciado para “{work.name if work else '?'}”: " + " · ".join(details))
        self.tick()
        return flow_id

    def resume(self, flow_id: int, accept_cost: bool = False) -> None:
        flow = self._db.flow(flow_id)
        if flow is None or flow.state != "pausado":
            return
        changes = {"state": "ativo", "message": None}
        if accept_cost:
            options = FlowOptions.from_json(flow.options)
            options.cost_confirmed = True
            changes["options"] = options.to_json()
            self._log(flow_id, "info", "Custo aceito pelo usuário; continuando.")
        else:
            self._log(flow_id, "info", "Continuando a pedido.")
        self._db.update_flow(flow_id, **changes)
        self.tick()

    def cancel(self, flow_id: int) -> None:
        """Para o fluxo. O que já foi feito fica (capítulos lidos, traduções salvas); um lote em andamento continua até
        o usuário pausá-lo ou cancelá-lo na janela do lote."""
        self._db.update_flow(flow_id, state="cancelado", message="Cancelado a pedido.")
        self._log(flow_id, "aviso", "Fluxo cancelado a pedido. O que já foi feito fica salvo.")
        self.changed.emit(flow_id)

    # --- máquina de estados ----------------------------------------------------------------

    def tick(self) -> None:
        for flow in self._db.flows(("ativo",)):
            for _ in range(len(STEPS)):  # avança várias etapas de uma vez se der (ex.: nada a ler, nada a traduzir)
                before = (flow.step, flow.state)
                flow = self._advance(flow)
                if flow is None or (flow.step, flow.state) == before or flow.state != "ativo":
                    break

    def _advance(self, flow: FlowRow) -> FlowRow | None:
        options = FlowOptions.from_json(flow.options)
        config = options.config(self._config())
        handler = getattr(self, f"_step_{flow.step}")
        handler(flow, options, config)
        return self._db.flow(flow.id)

    def _set(self, flow: FlowRow, **changes) -> None:
        self._db.update_flow(flow.id, **changes)
        self.changed.emit(flow.id)

    def _step_importar(self, flow: FlowRow, options: FlowOptions, config: Config) -> None:
        unread = self._db.unread_pages(options.chapter_ids)
        if not unread:
            self._log(flow.id, "ok", f"{len(options.chapter_ids)} capítulo(s) lido(s).")
            self._set(flow, step="custo", message=None)
            return
        if not self._importer.running:
            if self._importer.start(config):
                self._log(flow.id, "info", f"Lendo {unread} página(s): detecção dos balões e OCR, na sua GPU (sem custo de API)")
        message = f"Lendo {unread} página(s)…"
        if flow.message != message:
            self._set(flow, message=message)

    def _step_custo(self, flow: FlowRow, options: FlowOptions, config: Config) -> None:
        work = self._db.work(flow.work_id)
        estimate, cost = estimate_plan(self._db, config, work.id, work.source_lang, options.batch_options())
        if estimate.new_lines == 0:
            self._log(flow.id, "ok", "Nada a traduzir: todas as falas já tinham tradução salva.")
            self._set(flow, step="gerar", message="Nada a traduzir: todas as falas já tinham tradução salva.")
            return
        total = cost.total
        over = options.cost_limit is not None and (total is None or total > options.cost_limit)
        if over and not options.cost_confirmed:
            limit = pricing.format_cost(options.cost_limit).replace("menos de ", "")
            known = pricing.format_cost(total) if total is not None else "desconhecido (modelo sem preço na tabela)"
            message = (
                f"Custo estimado da tradução: {known}, acima do limite de {limit} "
                f"({estimate.new_lines} falas). Continue para traduzir mesmo assim, ou cancele o fluxo."
            )
            self._log(flow.id, "aviso", f"Pausado: custo estimado {known} acima do limite de {limit} ({estimate.new_lines} falas).")
            self._set(flow, state="pausado", message=message)
            self._notify(f"Fluxo pausado: o custo estimado ({known}) passa do limite de {limit}.", True)
            self.attention.emit(flow.id)
            return
        self._log(flow.id, "ok", f"Custo estimado: {pricing.format_cost(total)} para {estimate.new_lines} falas"
                  + (f" (limite {pricing.format_cost(options.cost_limit)})" if options.cost_limit is not None else ""))
        self._set(flow, step="traduzir", message=f"{estimate.new_lines} falas a traduzir · custo estimado {pricing.format_cost(total)}")

    def _step_traduzir(self, flow: FlowRow, options: FlowOptions, config: Config) -> None:
        if flow.batch_id is None:
            work = self._db.work(flow.work_id)
            estimate, cost = estimate_plan(self._db, config, work.id, work.source_lang, options.batch_options())
            batch_id = create_batch(self._db, config, work.id, work.source_lang, options.batch_options(), cost.total)
            self._log(flow.id, "info", f"Tradução em lote criada (lote {batch_id}): {estimate.new_lines} falas. O passo a passo segue abaixo.")
            self._set(flow, batch_id=batch_id, message=f"Traduzindo {estimate.new_lines} falas…")
            self._batch_runner.start(self._config())
            return
        state = self._db.batch_state(flow.batch_id)
        if state == "concluido":
            summary = self._db.batch_summary(flow.batch_id)
            self._log(flow.id, "ok", f"Tradução concluída (lote {flow.batch_id}) · custo real {pricing.format_cost(summary.cost) if summary.cost else 'US$ 0'}")
            self._set(flow, step="gerar", message=None)
        elif state == "cancelado":
            self._log(flow.id, "erro", "A tradução em lote foi cancelada; o fluxo parou.")
            self._set(flow, state="cancelado", message="A tradução em lote foi cancelada; o fluxo parou.")
            self.attention.emit(flow.id)
        elif state == "pausado":
            message = "A tradução está pausada. Use “Retomar tradução em lote” no menu; o fluxo continua sozinho quando ela terminar."
            if flow.message != message:
                self._log(flow.id, "aviso", "A tradução pausou; o fluxo espera ela ser retomada.")
                self._set(flow, message=message)
                self.attention.emit(flow.id)
        elif not self._batch_runner.running:
            # Ativo e ninguém rodando (ex.: depois de reabrir o app). Um lote já enviado à Batch API fica com a
            # conferência de 1 em 1 minuto do app: religar aqui consultaria a OpenAI a cada tique
            batch = next((b for b in self._db.batches(("ativo",)) if b.id == flow.batch_id), None)
            if batch is not None and not (batch.mode == "batch" and batch.remote_id):
                self._batch_runner.start(self._config())

    def _step_gerar(self, flow: FlowRow, options: FlowOptions, config: Config) -> None:
        if not options.generate:
            self._log(flow.id, "info", "Sem gerar arquivos (escolhido no assistente).")
            self._set(flow, step="fim")
            return
        if self._generating == flow.id or self._generator.running:
            return  # a nossa geração (ou a de outro) está rodando: espera o sinal de fim
        work = self._db.work(flow.work_id)
        if self._generator.start(config, work, options.chapter_ids, Path(options.folder), options.fmt, False, options.inpaint):
            self._generating = flow.id
            fmt = "pasta de imagens" if options.fmt == "pasta" else "CBZ"
            extra = ", reconstruindo o desenho" if options.inpaint else ""
            self._log(flow.id, "info", f"Gerando {len(options.chapter_ids)} capítulo(s) em {fmt} em {options.folder}{extra}")
            self._set(flow, message=f"Gerando {len(options.chapter_ids)} capítulo(s) em {options.folder}…")

    def _step_fim(self, flow: FlowRow, options: FlowOptions, config: Config) -> None:
        work = self._db.work(flow.work_id)
        done = f" {len(options.generated)} arquivo(s) em {options.folder}." if options.generated else ""
        self._log(flow.id, "ok", f"Fluxo concluído.{done}")
        self._set(flow, state="concluido", message=f"Concluído.{done}")
        self._notify(f"Fluxo de “{work.name if work else '?'}” concluído.{done}", False)
        self.attention.emit(flow.id)

    def generation_finished(self, summary) -> None:
        """Ligado ao fim da geração (Generator.finished)."""
        flow_id, self._generating = self._generating, None
        flow = self._db.flow(flow_id) if flow_id is not None else None
        if flow is None or flow.state != "ativo":
            self.tick()
            return
        options = FlowOptions.from_json(flow.options)
        for problem in summary.problems:
            self._log(flow.id, "aviso", f"Geração: {problem}")
        if summary.error or summary.cancelled:
            reason = summary.error or "geração parada a pedido"
            self._log(flow.id, "erro", f"A geração parou: {reason}")
            self._set(flow, state="pausado", message=f"A geração parou ({reason}). Continue para gerar de novo.")
            self.attention.emit(flow.id)
        else:
            options.generated = [str(p) for p in summary.files]
            left = f", {summary.untranslated} ficaram no original" if summary.untranslated else ""
            self._log(flow.id, "ok", f"Gerado: {len(summary.files)} arquivo(s), {summary.pages} página(s), {summary.drawn} falas desenhadas{left}")
            self._set(flow, step="fim", options=options.to_json())
        self.tick()
