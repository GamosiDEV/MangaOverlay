"""Tradução em lote: escolha dos capítulos (com custo estimado ao vivo) e janela de progresso."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME, pricing
from ..batch import LLM_ENGINES, TEXT_ENGINE, BatchOutcome, BatchProgress, batch_key, estimate
from ..config import ENGINES, Config
from ..db import Database, Work, normalize


class BatchDialog(QDialog):
    def __init__(self, db: Database, work: Work, config: Config, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Traduzir capítulos — {work.name} — {APP_DISPLAY_NAME}")
        self.resize(620, 560)
        self._db = db
        self._work = work
        self._config = config
        self._key = batch_key(config, work.id, work.source_lang)

        engine_note = ""
        if config.engine in TEXT_ENGINE:
            engine_note = " (no lote o texto já foi lido pelo OCR: vai no modo texto, mais barato)"
        model = f" · {self._key.model}" if self._key.engine in LLM_ENGINES else ""
        header = QLabel(f"<b>Motor:</b> {ENGINES[self._key.engine]}{model}{engine_note}")
        header.setWordWrap(True)

        self.chapters = QListWidget()
        chapter_ids = [c.id for c in db.chapters(work.id)]
        lines = db.chapter_lines(chapter_ids)
        for chapter in db.chapters(work.id):
            texts = [t for _p, t in lines.get(chapter.id, [])]
            done = len(db.find_translations(self._key, texts, fuzzy=False)) if texts else 0
            unique = len({normalize(t) for t in texts} - {""})
            status = f"{chapter.pages} págs · {len(texts)} falas"
            if chapter.read < chapter.pages:
                status += f" · ⚠ {chapter.pages - chapter.read} pág(s) ainda não lidas"
            if texts and done >= unique:
                status += " · já traduzido"
            item = QListWidgetItem(f"{chapter.name} — {status}")
            item.setData(Qt.ItemDataRole.UserRole, chapter.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if (not texts or done >= unique) else Qt.CheckState.Checked)
            self.chapters.addItem(item)
        self.chapters.itemChanged.connect(self._update_estimate)

        select_all = QPushButton("Marcar todos")
        select_all.clicked.connect(lambda: self._check_all(True))
        select_none = QPushButton("Desmarcar todos")
        select_none.clicked.connect(lambda: self._check_all(False))
        selection = QHBoxLayout()
        selection.addWidget(select_all)
        selection.addWidget(select_none)
        selection.addStretch(1)

        self.block = QSpinBox()
        self.block.setRange(1, 100)
        self.block.setValue(config.batch_pages_per_block)
        self.block.setSuffix(" páginas por pedido")
        self.block.valueChanged.connect(self._update_estimate)
        self.block_hint = QLabel("")
        block_row = QHBoxLayout()
        block_row.addWidget(self.block)
        block_row.addWidget(self.block_hint, 1)

        # Modo de envio: normal (na hora) ou Batch API da OpenAI (metade do preço, em até 24 h)
        self.mode_normal = QRadioButton("Envio normal: traduz agora, bloco a bloco")
        self.mode_batch = QRadioButton("Batch API da OpenAI: 50% mais barato; a OpenAI processa em segundo plano (em geral minutos, até 24 h)")
        self.mode_batch.setEnabled(self._key.engine == "openai-text")
        if not self.mode_batch.isEnabled():
            self.mode_batch.setToolTip("Disponível só com os motores da OpenAI.")
        (self.mode_batch if self.mode_batch.isEnabled() and config.batch_mode == "batch" else self.mode_normal).setChecked(True)
        self.mode_normal.toggled.connect(self._update_estimate)
        # Modo híbrido: o 1º capítulo na hora monta a memória antes do resto ir à OpenAI
        self.hybrid = QCheckBox("Traduzir o 1º capítulo marcado na hora, para montar a memória da obra antes de enviar o resto")
        self.hybrid.setChecked(db.memory(work.id).empty)
        self.hybrid.toggled.connect(self._update_estimate)
        mode_box = QVBoxLayout()
        mode_box.addWidget(self.mode_normal)
        mode_box.addWidget(self.mode_batch)
        hybrid_row = QHBoxLayout()
        hybrid_row.addSpacing(24)
        hybrid_row.addWidget(self.hybrid, 1)
        mode_box.addLayout(hybrid_row)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.start = self.buttons.addButton("Traduzir", QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(QLabel("Capítulos importados (as falas já traduzidas não são enviadas de novo):"))
        layout.addWidget(self.chapters, 1)
        layout.addLayout(selection)
        layout.addLayout(block_row)
        layout.addLayout(mode_box)
        layout.addWidget(self.summary)
        layout.addWidget(self.buttons)
        self.estimate = None
        self._update_estimate()

    def _check_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.chapters.count()):
            self.chapters.item(i).setCheckState(state)

    @property
    def mode(self) -> str:
        return "batch" if self.mode_batch.isChecked() else "normal"

    def sync_pages(self) -> int:
        """Páginas do 1º capítulo marcado, traduzidas na hora no modo híbrido (0 se não se aplica)."""
        chapters = self.selected_chapters()
        if self.mode != "batch" or not self.hybrid.isChecked() or len(chapters) < 2:
            return 0
        return len(self._db.chapter_pages(chapters[:1]))

    def selected_chapters(self) -> list[int]:
        return [
            self.chapters.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.chapters.count())
            if self.chapters.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _update_estimate(self) -> None:
        chapters = self.selected_chapters()
        if not chapters:
            self.estimate = None
            self.summary.setText("Marque ao menos um capítulo.")
            self.block_hint.setText("")
            self.start.setEnabled(False)
            return
        e = estimate(self._db, self._config, self._work.id, self._work.source_lang, chapters, self.block.value())
        self.estimate = e
        llm = self._key.engine in LLM_ENGINES
        too_big = llm and self.block.value() > e.max_pages_per_block
        self.block_hint.setText(
            f"<span style='color:#c0392b'>acima do máximo seguro para {self._key.model}: {e.max_pages_per_block}</span>"
            if too_big
            else (f"máximo seguro para {self._key.model}: {e.max_pages_per_block}" if llm else "")
        )
        repeated = e.lines - e.new_lines
        if e.new_lines == 0:
            text = "Tudo o que foi marcado já está traduzido."
        else:
            batch = self.mode == "batch"
            cost = pricing.format_cost(e.cost * (0.5 if batch else 1.0) if e.cost is not None else None) if llm else "grátis"
            if batch and e.cost is not None:
                cost += f" (no envio normal: {pricing.format_cost(e.cost)})"
            if llm and e.summary_cost:
                cost += f" + resumos da memória: {pricing.format_cost(e.summary_cost)}"
            text = (
                f"<b>{e.new_lines} falas a traduzir</b> em {e.requests} pedido(s), de {e.pages} páginas"
                + (f" ({repeated} já traduzidas ou repetidas não são enviadas)" if repeated else "")
                + f".<br><b>Custo estimado: {cost}</b>"
            )
            if llm:
                text += f" (~{pricing.thousands(e.input_tokens)} tokens de entrada, ~{pricing.thousands(e.output_tokens)} de saída)"
        self.summary.setText(text)
        self.hybrid.setVisible(self.mode == "batch")
        self.start.setEnabled(e.new_lines > 0 and not too_big)


class BatchWindow(QWidget):
    """Progresso da tradução em lote (não bloqueia o resto do app)."""

    pause_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(f"Tradução em lote — {APP_DISPLAY_NAME}")
        self.setMinimumWidth(480)
        self._title = QLabel("Preparando…")
        self._title.setWordWrap(True)
        self._bar = QProgressBar()
        self._details = QLabel("")
        self._details.setWordWrap(True)
        self._button = QPushButton("Pausar")
        self._button.clicked.connect(self._on_button)
        self._finished = False
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self._button)
        layout = QVBoxLayout(self)
        layout.addWidget(self._title)
        layout.addWidget(self._bar)
        layout.addWidget(self._details)
        layout.addLayout(buttons)

    def start(self, title: str) -> None:
        self._finished = False
        self._title.setText(title)
        self._bar.setRange(0, 0)  # indeterminada até o primeiro bloco
        self._details.setText("Pode fechar esta janela: a tradução continua em segundo plano.")
        self._button.setText("Pausar")
        self._button.setEnabled(True)
        self.show()
        self.raise_()

    def update_progress(self, progress: BatchProgress) -> None:
        self._bar.setRange(0, max(1, progress.total))
        self._bar.setValue(progress.done)
        details = [progress.message, f"custo até agora: {pricing.format_cost(progress.cost) if progress.cost else 'US$ 0'}"]
        if progress.failed:
            details.append(f"{progress.failed} bloco(s) com falas sem tradução")
        self._details.setText(" · ".join(details))

    def show_outcome(self, outcome: BatchOutcome) -> None:
        self._finished = True
        s = outcome.summary
        cost = pricing.format_cost(s.cost) if s.cost else "US$ 0"
        if outcome.state == "concluido":
            self._bar.setRange(0, max(1, s.requests))
            self._bar.setValue(s.requests)
            self._title.setText(f"Tradução concluída: {s.done} de {s.requests} bloco(s). Custo real: {cost}.")
            self._details.setText(
                f"{s.failed} bloco(s) ficaram com falas sem tradução: use “Retomar tradução em lote” no menu para tentar de novo."
                if s.failed
                else "Abra as páginas na tela e aperte o atalho de traduzir: a tradução aparece na hora, sem nova cobrança."
            )
        elif outcome.state == "pausado":
            self._title.setText(f"Tradução pausada ({s.done} de {s.requests} bloco(s) prontos). Custo até agora: {cost}.")
            self._details.setText(
                (f"Motivo: {outcome.error}\n" if outcome.error else "")
                + "Continue por “Retomar tradução em lote” no menu da bandeja."
            )
        else:
            self._title.setText("Tradução interrompida; continua quando o app abrir de novo.")
        self._button.setText("Fechar")
        self._button.setEnabled(True)

    def _on_button(self) -> None:
        if self._finished:
            self.hide()
            return
        self._button.setEnabled(False)
        self._button.setText("Pausando…")
        self.pause_requested.emit()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
