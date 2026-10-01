"""Tradução em lote: escolha dos capítulos (com custo estimado ao vivo) e janela de progresso."""

import html
import time

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME, pricing
from ..batch import (
    LLM_ENGINES,
    LOG_FILE,
    TEXT_ENGINE,
    BatchLogEntry,
    BatchOptions,
    BatchOutcome,
    BatchProgress,
    batch_key,
    estimate_plan,
    split_parts,
)
from ..config import ENGINES, Config
from ..db import Database, Work, normalize


class TranslationOptions(QWidget):
    """Como os capítulos vão ser traduzidos: páginas por pedido, modo de envio, capítulos de memória, partes da Batch
    API e nomes, com o custo estimado ao vivo. Usado na janela do lote e no fluxo completo.

    `chapters()` devolve os capítulos escolhidos (só os já lidos entram na estimativa; `unread_note` explica o resto).
    """

    changed = Signal()

    def __init__(self, db: Database, work: Work, config: Config, chapters, parent: QWidget | None = None):
        super().__init__(parent)
        self._db = db
        self._work = work
        self._config = config
        self._chapters = chapters
        self._key = batch_key(config, work.id, work.source_lang)
        llm = self._key.engine in LLM_ENGINES

        self.block = QSpinBox()
        self.block.setRange(1, 100)
        self.block.setValue(config.batch_pages_per_block)
        self.block.setSuffix(" páginas por pedido")
        self.block_hint = QLabel("")
        block_row = QHBoxLayout()
        block_row.addWidget(self.block)
        block_row.addWidget(self.block_hint, 1)

        # Modo de envio: normal (na hora) ou Batch API da OpenAI (metade do preço, em até 24 h)
        self.mode_normal = QRadioButton("Envio normal: traduz agora, bloco a bloco (a memória se atualiza a cada capítulo)")
        self.mode_batch = QRadioButton("Batch API da OpenAI: 50% mais barato; a OpenAI processa em segundo plano (em geral minutos, até 24 h)")
        self.mode_batch.setEnabled(self._key.engine == "openai-text")
        if not self.mode_batch.isEnabled():
            self.mode_batch.setToolTip("Disponível só com os motores da OpenAI.")
        (self.mode_batch if self.mode_batch.isEnabled() and config.batch_mode == "batch" else self.mode_normal).setChecked(True)

        # Batch API: capítulos de memória (na hora) e o resto em partes, com a memória atualizada entre elas
        self.memory_chapters = QSpinBox()
        self.memory_chapters.setRange(0, 999)
        self.memory_chapters.setValue(config.batch_memory_chapters if db.memory(work.id).empty else 0)
        self.memory_chapters.setSuffix(" capítulo(s)")
        self.memory_own_block = QCheckBox("em pedidos de")
        self.memory_own_block.setToolTip("Sem marcar, os capítulos de memória usam o mesmo tamanho de pedido do lote.")
        self.memory_block = QSpinBox()
        self.memory_block.setRange(1, 100)
        self.memory_block.setSuffix(" páginas")
        self.memory_own_block.setChecked(config.batch_memory_block > 0)
        self.memory_block.setValue(config.batch_memory_block or config.batch_pages_per_block)
        memory_row = QHBoxLayout()
        memory_row.addSpacing(24)
        memory_row.addWidget(QLabel("Antes do envio, traduzir na hora os primeiros"))
        memory_row.addWidget(self.memory_chapters)
        memory_row.addWidget(self.memory_own_block)
        memory_row.addWidget(self.memory_block)
        memory_row.addStretch(1)
        self.parts = QSpinBox()
        self.parts.setRange(1, 999)
        self.parts.setValue(config.batch_parts)
        self.parts.setSuffix(" parte(s)")
        parts_row = QHBoxLayout()
        parts_row.addSpacing(24)
        parts_row.addWidget(QLabel("Enviar o resto em"))
        parts_row.addWidget(self.parts)
        parts_row.addWidget(QLabel("(a memória é atualizada depois de cada parte)"), 1)
        self._batch_rows = QWidget()
        batch_rows = QVBoxLayout(self._batch_rows)
        batch_rows.setContentsMargins(0, 0, 0, 0)
        batch_rows.addLayout(memory_row)
        batch_rows.addLayout(parts_row)

        self.survey_names = QCheckBox("Levantar os nomes dos personagens antes de traduzir cada grupo (salvos direto na lista)")
        self.survey_names.setToolTip(
            "Usa o modelo barato do levantamento completo, só nos capítulos ainda não analisados. "
            "A lista pode ser revisada depois em “Personagens da obra”."
        )
        self.survey_names.setChecked(config.batch_survey_names and llm)
        self.survey_names.setEnabled(llm)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(block_row)
        layout.addWidget(self.mode_normal)
        layout.addWidget(self.mode_batch)
        layout.addWidget(self._batch_rows)
        layout.addWidget(self.survey_names)
        layout.addWidget(self.summary)

        for spin in (self.block, self.memory_chapters, self.memory_block, self.parts):
            spin.valueChanged.connect(self.refresh)
        for check in (self.mode_normal, self.memory_own_block, self.survey_names):
            check.toggled.connect(self.refresh)
        self.estimate = None
        self.cost = None
        self.ok = False

    @property
    def mode(self) -> str:
        return "batch" if self.mode_batch.isChecked() else "normal"

    @property
    def key(self):
        return self._key

    def options(self, chapter_ids: list[int] | None = None) -> BatchOptions:
        batch = self.mode == "batch"
        return BatchOptions(
            chapter_ids=list(self._chapters() if chapter_ids is None else chapter_ids),
            pages_per_block=self.block.value(),
            mode=self.mode,
            memory_chapters=self.memory_chapters.value() if batch else 0,
            memory_block=self.memory_block.value() if batch and self.memory_own_block.isChecked() else 0,
            parts=self.parts.value() if batch else 1,
            survey_names=self.survey_names.isChecked() and self.survey_names.isEnabled(),
        )

    def remember(self) -> dict:
        """As escolhas como configuração (padrão da próxima vez)."""
        return {
            "batch_pages_per_block": self.block.value(),
            "batch_mode": self.mode,
            "batch_memory_chapters": self.memory_chapters.value(),
            "batch_memory_block": self.memory_block.value() if self.memory_own_block.isChecked() else 0,
            "batch_parts": self.parts.value(),
            "batch_survey_names": self.survey_names.isChecked(),
        }

    def refresh(self) -> None:
        batch = self.mode == "batch"
        self._batch_rows.setVisible(batch)
        self.memory_block.setEnabled(self.memory_own_block.isChecked())
        chapters = self._chapters()
        if not chapters:
            self.estimate, self.cost, self.ok = None, None, False
            self.summary.setText("Marque ao menos um capítulo.")
            self.block_hint.setText("")
            self.changed.emit()
            return
        options = self.options(chapters)
        e, cost = estimate_plan(self._db, self._config, self._work.id, self._work.source_lang, options)
        self.estimate, self.cost = e, cost
        llm = self._key.engine in LLM_ENGINES
        too_big = llm and self.block.value() > e.max_pages_per_block
        self.block_hint.setText(
            f"<span style='color:#c0392b'>acima do máximo seguro para {self._key.model}: {e.max_pages_per_block}</span>"
            if too_big
            else (f"máximo seguro para {self._key.model}: {e.max_pages_per_block}" if llm else "")
        )
        memory, rest = options.memory_and_rest()
        if batch:
            self.parts.setMaximum(max(1, len(rest)))
            self.memory_chapters.setMaximum(max(0, len(chapters)))
        repeated = e.lines - e.new_lines
        if e.new_lines == 0:
            text = "Tudo o que foi marcado já está traduzido."
        else:
            text = (
                f"<b>{e.new_lines} falas a traduzir</b>, de {e.pages} páginas"
                + (f" ({repeated} já traduzidas ou repetidas não são enviadas)" if repeated else "")
                + "."
            )
            if llm:
                parts = []
                if memory:
                    parts.append(f"{len(memory)} capítulo(s) de memória: {pricing.format_cost(cost.memory)}")
                label = "Batch API" + (f" em {len(split_parts([1] * len(rest), options.parts))} partes" if options.parts > 1 else "")
                parts.append(f"{label}: {pricing.format_cost(cost.main)}" if batch else f"tradução: {pricing.format_cost(cost.main)}")
                if options.survey_names:
                    parts.append(f"nomes: {pricing.format_cost(cost.names)}")
                if cost.summaries:
                    parts.append(f"resumos: {pricing.format_cost(cost.summaries)}")
                text += f"<br><b>Custo estimado: {pricing.format_cost(cost.total)}</b> ({'; '.join(parts)})"
                text += f"<br>~{pricing.thousands(e.input_tokens)} tokens de entrada, ~{pricing.thousands(e.output_tokens)} de saída."
                if cost.names_error:
                    text += f"<br><span style='color:#d35400'>Nomes: {html.escape(cost.names_error)}</span>"
            else:
                text += "<br><b>Custo: grátis.</b>"
        self.summary.setText(text)
        self.ok = e.new_lines > 0 and not too_big
        self.changed.emit()


class BatchDialog(QDialog):
    def __init__(self, db: Database, work: Work, config: Config, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Traduzir capítulos — {work.name} — {APP_DISPLAY_NAME}")
        self.resize(680, 640)
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

        select_all = QPushButton("Marcar todos")
        select_all.clicked.connect(lambda: self._check_all(True))
        select_none = QPushButton("Desmarcar todos")
        select_none.clicked.connect(lambda: self._check_all(False))
        selection = QHBoxLayout()
        selection.addWidget(select_all)
        selection.addWidget(select_none)
        selection.addStretch(1)

        self.options = TranslationOptions(db, work, config, self.selected_chapters)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.start = self.buttons.addButton("Traduzir", QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(QLabel("Capítulos importados (as falas já traduzidas não são enviadas de novo):"))
        layout.addWidget(self.chapters, 1)
        layout.addLayout(selection)
        layout.addWidget(self.options)
        layout.addWidget(self.buttons)
        self.options.changed.connect(lambda: self.start.setEnabled(self.options.ok))
        self.chapters.itemChanged.connect(lambda _item: self.options.refresh())
        self.options.refresh()

    def _check_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.chapters.count()):
            self.chapters.item(i).setCheckState(state)

    def selected_chapters(self) -> list[int]:
        return [
            self.chapters.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.chapters.count())
            if self.chapters.item(i).checkState() == Qt.CheckState.Checked
        ]


class BatchWindow(QWidget):
    """Progresso da tradução em lote (não bloqueia o resto do app).

    "Mostrar detalhes" expande um log com o passo a passo: cada bloco enviado, quanto voltou, tempo, tokens e
    custo, novas tentativas, falas que o modelo pulou, memória da obra e erros. O mesmo log vai para um arquivo.
    """

    pause_requested = Signal()
    cancel_requested = Signal(int)  # id do lote a descartar

    _MAX_ENTRIES = 3000
    _LEVELS = {  # nível -> (símbolo, cor)
        "info": ("•", ""),
        "ok": ("✓", "#2e8b57"),
        "aviso": ("⚠", "#d35400"),
        "erro": ("✖", "#c0392b"),
    }

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(f"Tradução em lote — {APP_DISPLAY_NAME}")
        self.setMinimumWidth(560)
        self._title = QLabel("Preparando…")
        self._title.setWordWrap(True)
        self._bar = QProgressBar()
        self._details = QLabel("")
        self._details.setWordWrap(True)
        self._timing = QLabel("")
        self._timing.setStyleSheet("color: palette(placeholder-text);")
        self._button = QPushButton("Pausar")
        self._button.clicked.connect(self._on_button)
        self._cancel_button = QPushButton("Cancelar lote…")
        self._cancel_button.setToolTip("Descarta o lote: não continua nem aparece em “Retomar”. O que já foi traduzido fica salvo.")
        self._cancel_button.clicked.connect(self._on_cancel)
        self._cancel_button.setVisible(False)
        self._batch_id: int | None = None
        self._toggle = QPushButton()
        self._toggle.setCheckable(True)
        self._toggle.toggled.connect(self._set_expanded)
        self._finished = False
        self._started_at: float | None = None
        self._first_done: int | None = None  # blocos já prontos quando esta execução começou (retomada)
        self._entries: list[BatchLogEntry] = []
        self._warnings = 0
        self._errors = 0
        self._last_done = 0
        self._last_total = 0

        # Painel de detalhes
        self._counters = QLabel("")
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._log_view.setMaximumBlockCount(self._MAX_ENTRIES)
        self._log_view.setPlaceholderText("Nada registrado ainda.")
        self._only_problems = QCheckBox("Só avisos e erros")
        self._only_problems.toggled.connect(self._render_log)
        copy = QPushButton("Copiar log")
        copy.clicked.connect(self._copy_log)
        open_file = QPushButton("Abrir arquivo de log")
        open_file.setToolTip(str(LOG_FILE))
        open_file.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_FILE))))
        log_buttons = QHBoxLayout()
        log_buttons.addWidget(self._only_problems)
        log_buttons.addStretch(1)
        log_buttons.addWidget(copy)
        log_buttons.addWidget(open_file)
        self._panel = QWidget()
        panel = QVBoxLayout(self._panel)
        panel.setContentsMargins(0, 0, 0, 0)
        panel.addWidget(self._counters)
        panel.addWidget(self._log_view, 1)
        panel.addLayout(log_buttons)
        self._panel.setVisible(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self._toggle)
        buttons.addStretch(1)
        buttons.addWidget(self._cancel_button)
        buttons.addWidget(self._button)
        layout = QVBoxLayout(self)
        layout.addWidget(self._title)
        layout.addWidget(self._bar)
        layout.addWidget(self._details)
        layout.addWidget(self._timing)
        layout.addLayout(buttons)
        layout.addWidget(self._panel, 1)
        self._update_toggle()

        # Tempo decorrido e estimativa atualizados a cada segundo, mesmo sem novidades do lote
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._update_timing)

    @property
    def has_log(self) -> bool:
        return bool(self._entries)

    def start(self, title: str) -> None:
        self._finished = False
        self._title.setText(title)
        self._bar.setRange(0, 0)  # indeterminada até o primeiro bloco
        self._details.setText("Pode fechar esta janela: a tradução continua em segundo plano.")
        self._button.setText("Pausar")
        self._button.setEnabled(True)
        self._cancel_button.setVisible(False)  # aparece quando o primeiro bloco disser qual é o lote
        self._batch_id = None
        self._started_at = time.monotonic()
        self._first_done = None
        self._last_done = self._last_total = 0
        self._clock.start()
        self._update_timing()
        self.show()
        self.raise_()

    def update_progress(self, progress: BatchProgress) -> None:
        if self._started_at is None:  # retomada automática ao abrir o app, sem start()
            self._started_at = time.monotonic()
            self._clock.start()
        if self._first_done is None:
            self._first_done = progress.done
        if not self._finished:
            self._batch_id = progress.batch_id
            self._cancel_button.setVisible(True)
            self._cancel_button.setEnabled(True)
        self._last_done, self._last_total = progress.done, progress.total
        self._bar.setRange(0, max(1, progress.total))
        self._bar.setValue(progress.done)
        details = [progress.message, f"custo até agora: {pricing.format_cost(progress.cost) if progress.cost else 'US$ 0'}"]
        if progress.failed:
            details.append(f"{progress.failed} bloco(s) com falas sem tradução")
        self._details.setText(" · ".join(details))
        self._update_timing()

    def add_log(self, entry: BatchLogEntry) -> None:
        self._entries.append(entry)
        if len(self._entries) > self._MAX_ENTRIES:
            del self._entries[: len(self._entries) - self._MAX_ENTRIES]
        self._warnings += entry.level == "aviso"
        self._errors += entry.level == "erro"
        if not self._only_problems.isChecked() or entry.level in ("aviso", "erro"):
            self._append(entry)
        self._update_toggle()

    def show_outcome(self, outcome: BatchOutcome) -> None:
        self._finished = True
        self._batch_id = outcome.batch_id
        # Pausado dá para descartar; concluído ou interrompido (continua ao abrir o app), não
        self._cancel_button.setVisible(outcome.state == "pausado")
        self._cancel_button.setEnabled(True)
        self._clock.stop()
        self._update_timing()
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
            if outcome.error:
                self._toggle.setChecked(True)  # pausou por erro: mostra o que aconteceu antes
        else:
            self._title.setText("Tradução interrompida; continua quando o app abrir de novo.")
        self._button.setText("Fechar")
        self._button.setEnabled(True)

    # --- detalhes ---------------------------------------------------------------------

    def _set_expanded(self, expanded: bool) -> None:
        self._panel.setVisible(expanded)
        self._update_toggle()
        if expanded:
            self.resize(max(self.width(), 760), max(self.height(), 560))
            self._log_view.verticalScrollBar().setValue(self._log_view.verticalScrollBar().maximum())
        else:
            self.adjustSize()

    def _update_toggle(self) -> None:
        problems = []
        if self._errors:
            problems.append(f"{self._errors} erro(s)")
        if self._warnings:
            problems.append(f"{self._warnings} aviso(s)")
        extra = f" ({', '.join(problems)})" if problems else ""
        arrow = "▴" if self._toggle.isChecked() else "▾"
        self._toggle.setText(f"{'Esconder' if self._toggle.isChecked() else 'Mostrar'} detalhes{extra} {arrow}")
        self._counters.setText(
            f"{len(self._entries)} registro(s) · {self._warnings} aviso(s) · {self._errors} erro(s)"
            + (f" · blocos: {self._last_done} de {self._last_total}" if self._last_total else "")
        )

    def _format(self, entry: BatchLogEntry) -> str:
        symbol, color = self._LEVELS.get(entry.level, ("•", ""))
        text = html.escape(entry.message)
        if color:
            text = f"<span style='color:{color}'>{symbol} {text}</span>"
        else:
            text = f"{symbol} {text}"
        return f"<span style='color:gray'>{entry.time:%H:%M:%S}</span> {text}"

    def _append(self, entry: BatchLogEntry) -> None:
        bar = self._log_view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self._log_view.appendHtml(self._format(entry))
        if at_bottom:  # não arrasta a rolagem de quem está lendo algo mais acima
            bar.setValue(bar.maximum())

    def _render_log(self) -> None:
        self._log_view.clear()
        only = self._only_problems.isChecked()
        for entry in self._entries:
            if not only or entry.level in ("aviso", "erro"):
                self._append(entry)

    def _copy_log(self) -> None:
        QGuiApplication.clipboard().setText(
            "\n".join(f"{e.time:%Y-%m-%d %H:%M:%S} [{e.level}] {e.message}" for e in self._entries)
        )

    def _update_timing(self) -> None:
        if self._started_at is None:
            self._timing.setText("")
            return
        elapsed = time.monotonic() - self._started_at
        text = f"Tempo decorrido: {_duration(elapsed)}"
        done_now = self._last_done - (self._first_done or 0)
        remaining = self._last_total - self._last_done
        if not self._finished and done_now > 0 and remaining > 0:
            text += f" · faltam cerca de {_duration(elapsed / done_now * remaining)}"
        self._timing.setText(text)

    def show_cancelled(self, summary) -> None:
        cost = pricing.format_cost(summary.cost) if summary.cost else "US$ 0"
        self._finished = True
        self._clock.stop()
        self._title.setText(f"Lote cancelado ({summary.done} de {summary.requests} bloco(s) prontos). Custo: {cost}.")
        self._details.setText("As falas já traduzidas continuam salvas e aparecem na leitura, sem nova cobrança.")
        self._cancel_button.setVisible(False)
        self._button.setText("Fechar")
        self._button.setEnabled(True)

    def _on_cancel(self) -> None:
        if self._batch_id is None:
            return
        answer = QMessageBox.question(
            self,
            APP_DISPLAY_NAME,
            "Cancelar este lote?\n\nEle para agora e não aparece mais em “Retomar tradução em lote”. As falas já "
            "traduzidas continuam salvas. Na Batch API, o pedido também é cancelado na OpenAI.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._cancel_button.setEnabled(False)
        self._cancel_button.setText("Cancelando…")
        self.cancel_requested.emit(self._batch_id)

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


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"
