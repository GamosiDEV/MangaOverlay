"""Fluxo completo: importar os capítulos → conferir o custo → traduzir → gerar o resultado, uma etapa atrás da outra.

O fluxo não faz o trabalho: ele usa o importador, a tradução em lote e a geração que o app já tem, e só decide quando
começar cada um. Tudo o que ele sabe fica no banco (tabela `fluxos`): a etapa atual e as escolhas feitas no assistente.
Por isso fechar o app no meio (por exemplo, esperando a Batch API, que leva até 24 h) não perde nada: ao abrir de novo,
`tick()` olha o banco e continua de onde parou.

Custo: capítulos recém-importados só têm custo conhecido depois da leitura. Se a estimativa passar do limite escolhido,
o fluxo pausa e pergunta; abaixo dele, segue sozinho.
"""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from . import pricing
from .batch import BatchOptions, create_batch, estimate_plan
from .config import Config
from .db import Database, FlowRow

STEPS = ("importar", "custo", "traduzir", "gerar", "fim")
STEP_LABELS = {
    "importar": "Ler os capítulos (detecção e OCR)",
    "custo": "Conferir o custo",
    "traduzir": "Traduzir",
    "gerar": "Gerar o resultado",
    "fim": "Concluído",
}


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

    # --- ações do usuário ------------------------------------------------------------

    def start(self, work_id: int, options: FlowOptions) -> int:
        flow_id = self._db.create_flow(work_id, options.to_json())
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
        self._db.update_flow(flow_id, **changes)
        self.tick()

    def cancel(self, flow_id: int) -> None:
        """Para o fluxo. O que já foi feito fica (capítulos lidos, traduções salvas); um lote em andamento continua até
        o usuário pausá-lo ou cancelá-lo na janela do lote."""
        self._db.update_flow(flow_id, state="cancelado", message="Cancelado a pedido.")
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
            self._set(flow, step="custo", message=None)
            return
        if not self._importer.running:
            self._importer.start(config)
        message = f"Lendo {unread} página(s)…"
        if flow.message != message:
            self._set(flow, message=message)

    def _step_custo(self, flow: FlowRow, options: FlowOptions, config: Config) -> None:
        work = self._db.work(flow.work_id)
        estimate, cost = estimate_plan(self._db, config, work.id, work.source_lang, options.batch_options())
        if estimate.new_lines == 0:
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
            self._set(flow, state="pausado", message=message)
            self._notify(f"Fluxo pausado: o custo estimado ({known}) passa do limite de {limit}.", True)
            return
        self._set(flow, step="traduzir", message=f"{estimate.new_lines} falas a traduzir · custo estimado {pricing.format_cost(total)}")

    def _step_traduzir(self, flow: FlowRow, options: FlowOptions, config: Config) -> None:
        if flow.batch_id is None:
            work = self._db.work(flow.work_id)
            estimate, cost = estimate_plan(self._db, config, work.id, work.source_lang, options.batch_options())
            batch_id = create_batch(self._db, config, work.id, work.source_lang, options.batch_options(), cost.total)
            self._set(flow, batch_id=batch_id, message=f"Traduzindo {estimate.new_lines} falas…")
            self._batch_runner.start(self._config())
            return
        state = self._db.batch_state(flow.batch_id)
        if state == "concluido":
            self._set(flow, step="gerar", message=None)
        elif state == "cancelado":
            self._set(flow, state="cancelado", message="A tradução em lote foi cancelada; o fluxo parou.")
        elif state == "pausado":
            message = "A tradução está pausada. Use “Retomar tradução em lote” no menu; o fluxo continua sozinho quando ela terminar."
            if flow.message != message:
                self._set(flow, message=message)
        elif not self._batch_runner.running:
            # Ativo e ninguém rodando (ex.: depois de reabrir o app). Um lote já enviado à Batch API fica com a
            # conferência de 1 em 1 minuto do app: religar aqui consultaria a OpenAI a cada tique
            batch = next((b for b in self._db.batches(("ativo",)) if b.id == flow.batch_id), None)
            if batch is not None and not (batch.mode == "batch" and batch.remote_id):
                self._batch_runner.start(self._config())

    def _step_gerar(self, flow: FlowRow, options: FlowOptions, config: Config) -> None:
        if not options.generate:
            self._set(flow, step="fim")
            return
        if self._generating == flow.id or self._generator.running:
            return  # a nossa geração (ou a de outro) está rodando: espera o sinal de fim
        work = self._db.work(flow.work_id)
        if self._generator.start(config, work, options.chapter_ids, Path(options.folder), options.fmt, False, options.inpaint):
            self._generating = flow.id
            self._set(flow, message=f"Gerando {len(options.chapter_ids)} capítulo(s) em {options.folder}…")

    def _step_fim(self, flow: FlowRow, options: FlowOptions, config: Config) -> None:
        work = self._db.work(flow.work_id)
        done = f" {len(options.generated)} arquivo(s) em {options.folder}." if options.generated else ""
        self._set(flow, state="concluido", message=f"Concluído.{done}")
        self._notify(f"Fluxo de “{work.name if work else '?'}” concluído.{done}", False)

    def generation_finished(self, summary) -> None:
        """Ligado ao fim da geração (Generator.finished)."""
        flow_id, self._generating = self._generating, None
        flow = self._db.flow(flow_id) if flow_id is not None else None
        if flow is None or flow.state != "ativo":
            self.tick()
            return
        options = FlowOptions.from_json(flow.options)
        if summary.error or summary.cancelled:
            reason = summary.error or "geração parada a pedido"
            self._set(flow, state="pausado", message=f"A geração parou ({reason}). Continue para gerar de novo.")
        else:
            options.generated = [str(p) for p in summary.files]
            self._set(flow, step="fim", options=options.to_json())
        self.tick()
