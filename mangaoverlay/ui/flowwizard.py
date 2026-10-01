"""Fluxo completo: o assistente que junta todas as escolhas (obra → capítulos → tradução → resultado) e a janela que
mostra o andamento das etapas (ver flow.py)."""

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from .. import APP_DISPLAY_NAME, pricing
from ..batch import LLM_ENGINES
from ..config import CLAUDE_MODELS, ENGINES, OPENAI_MODELS, Config
from ..db import Database, Work
from ..flow import STEP_LABELS, STEPS, FlowOptions
from ..languages import SOURCES, source_name
from ..sources import ChapterSource, discover
from .batchdialog import TranslationOptions

# No lote o texto já foi lido pelo OCR: os motores "lê a imagem" vão no modo texto
_FLOW_ENGINES = ["local", "google", "openai-text", "claude-text"]


class FlowWizard(QWizard):
    """Depois de aceito: `work()` cria a obra (se for nova) e `chapter_ids()` registra os capítulos novos; `options()`
    devolve as escolhas para o executor do fluxo."""

    def __init__(self, db: Database, config: Config, current_work: int | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Fluxo completo — {APP_DISPLAY_NAME}")
        self.resize(760, 640)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage)
        self.setButtonText(QWizard.WizardButton.FinishButton, "Começar")
        self.setButtonText(QWizard.WizardButton.NextButton, "Próximo >")
        self.setButtonText(QWizard.WizardButton.BackButton, "< Voltar")
        self.setButtonText(QWizard.WizardButton.CancelButton, "Cancelar")
        self._db = db
        self._config = config
        self.work_page = _WorkPage(db, config, current_work)
        self.chapters_page = _ChaptersPage(db, self.work_page)
        self.translation_page = _TranslationPage(db, config, self.work_page, self.chapters_page)
        self.result_page = _ResultPage(config)
        self.summary_page = _SummaryPage(self)
        for page in (self.work_page, self.chapters_page, self.translation_page, self.result_page, self.summary_page):
            self.addPage(page)

    def work(self) -> Work:
        page = self.work_page
        if page.existing.isChecked():
            return page.selected_work()
        return self._db.create_work(page.name.text().strip(), page.language.currentData())

    def chapter_ids(self, work: Work) -> list[int]:
        """Os capítulos escolhidos, já registrados na obra (os novos ficam pendentes de leitura), na ordem da obra."""
        ids = set(self.chapters_page.checked_existing())
        for source in self.chapters_page.new_sources:
            chapter_id = self._db.add_chapter(work.id, source.name, source.order, source.origin, source.files)
            if chapter_id is None:  # já estava importado nesta obra
                chapter_id = self._db.chapter_by_origin(work.id, source.origin)
            if chapter_id is not None:
                ids.add(chapter_id)
        return [c.id for c in self._db.chapters(work.id) if c.id in ids]

    def options(self, chapter_ids: list[int]) -> FlowOptions:
        t = self.translation_page
        batch = t.widget.options(chapter_ids)
        r = self.result_page
        return FlowOptions(
            chapter_ids=chapter_ids,
            engine=t.engine.currentData(),
            openai_model=t.openai_model.currentText().strip() or OPENAI_MODELS[0],
            claude_model=t.claude_model.currentText().strip() or CLAUDE_MODELS[0],
            pages_per_block=batch.pages_per_block,
            mode=batch.mode,
            memory_chapters=batch.memory_chapters,
            memory_block=batch.memory_block,
            parts=batch.parts,
            survey_names=batch.survey_names,
            cost_limit=t.limit.value() if t.use_limit.isChecked() and t.engine.currentData() in LLM_ENGINES else None,
            generate=r.generate.isChecked(),
            fmt="pasta" if r.folder_format.isChecked() else "cbz",
            folder=r.folder.text().strip(),
            inpaint=r.inpaint.isChecked(),
        )

    def remember(self) -> dict:
        """Escolhas que viram o padrão da próxima vez (as mesmas da janela do lote e da geração)."""
        t, r = self.translation_page, self.result_page
        values = t.widget.remember()
        values.update(
            generate_format="pasta" if r.folder_format.isChecked() else "cbz",
            generate_inpaint=r.inpaint.isChecked(),
            flow_cost_limit=t.limit.value(),
            flow_generate=r.generate.isChecked(),
        )
        if r.folder.text().strip():
            values["generate_dir"] = r.folder.text().strip()
        return values


class _WorkPage(QWizardPage):
    def __init__(self, db: Database, config: Config, current_work: int | None):
        super().__init__()
        self.setTitle("1. Obra")
        self.setSubTitle("Escolha uma obra que você já tem ou crie uma nova.")
        self._db = db
        self.existing = QRadioButton("Obra existente:")
        self.works = QComboBox()
        for work in db.works():
            self.works.addItem(f"{work.name} · {source_name(work.source_lang)}", work.id)
        self.works.setCurrentIndex(max(0, self.works.findData(current_work)))
        self.new = QRadioButton("Nova obra:")
        self.name = QLineEdit()
        self.name.setPlaceholderText("Nome do mangá, manhwa ou manhua")
        self.language = QComboBox()
        for code in SOURCES:
            self.language.addItem(source_name(code), code)
        self.language.setCurrentIndex(max(0, self.language.findData(config.source_lang)))
        group = QButtonGroup(self)
        group.addButton(self.existing)
        group.addButton(self.new)
        (self.existing if self.works.count() else self.new).setChecked(True)
        self.existing.setEnabled(self.works.count() > 0)
        new_form = QFormLayout()
        new_form.addRow("Nome:", self.name)
        new_form.addRow("Idioma original:", self.language)
        layout = QVBoxLayout(self)
        layout.addWidget(self.existing)
        layout.addWidget(self.works)
        layout.addSpacing(12)
        layout.addWidget(self.new)
        layout.addLayout(new_form)
        layout.addStretch(1)
        for widget in (self.existing, self.new):
            widget.toggled.connect(self._update)
        self.works.currentIndexChanged.connect(lambda _i: self.completeChanged.emit())
        self.name.textChanged.connect(lambda _t: self.completeChanged.emit())
        self._update()

    def _update(self) -> None:
        self.works.setEnabled(self.existing.isChecked())
        self.name.setEnabled(self.new.isChecked())
        self.language.setEnabled(self.new.isChecked())
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        if self.existing.isChecked():
            return self.works.currentData() is not None
        name = self.name.text().strip()
        return bool(name) and all(w.name.casefold() != name.casefold() for w in self._db.works())

    def selected_work(self) -> Work | None:
        return self._db.work(self.works.currentData()) if self.existing.isChecked() else None

    def preview(self) -> Work:
        """A obra escolhida, ou uma provisória (id 0, sem nada no banco) enquanto a nova não é criada."""
        return self.selected_work() or Work(0, self.name.text().strip(), self.language.currentData())


class _ChaptersPage(QWizardPage):
    def __init__(self, db: Database, work_page: _WorkPage):
        super().__init__()
        self.setTitle("2. Capítulos")
        self.setSubTitle("Marque capítulos já importados e/ou adicione novos (pasta de imagens, CBZ, ZIP ou PDF).")
        self._db = db
        self._work_page = work_page
        self.new_sources: list[ChapterSource] = []
        self.existing = QListWidget()
        self.existing.itemChanged.connect(lambda _i: self.completeChanged.emit())
        self.added = QListWidget()
        files = QPushButton("Adicionar arquivos…")
        files.clicked.connect(self._add_files)
        folder = QPushButton("Adicionar pasta…")
        folder.setToolTip("Uma pasta de imagens é um capítulo; uma pasta com subpastas, CBZs ou PDFs vira vários.")
        folder.clicked.connect(self._add_folder)
        remove = QPushButton("Remover da lista")
        remove.clicked.connect(self._remove)
        buttons = QHBoxLayout()
        for button in (files, folder, remove):
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.existing_label = QLabel("<b>Já importados nesta obra</b>")
        layout = QVBoxLayout(self)
        layout.addWidget(self.existing_label)
        layout.addWidget(self.existing, 1)
        layout.addWidget(QLabel("<b>Novos (serão importados e lidos na sua GPU, sem custo de API)</b>"))
        layout.addWidget(self.added, 1)
        layout.addLayout(buttons)

    def initializePage(self) -> None:
        self.existing.clear()
        work = self._work_page.selected_work()
        chapters = self._db.chapters(work.id) if work else []
        for chapter in chapters:
            item = QListWidgetItem(f"{chapter.name} — {chapter.pages} págs · {chapter.texts} falas")
            item.setData(Qt.ItemDataRole.UserRole, chapter.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.existing.addItem(item)
        self.existing.setVisible(bool(chapters))
        self.existing_label.setVisible(bool(chapters))
        self.completeChanged.emit()

    def checked_existing(self) -> list[int]:
        return [
            self.existing.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.existing.count())
            if self.existing.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _add_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(
            self, "Capítulos", str(Path.home()), "Capítulos (*.cbz *.zip *.pdf *.png *.jpg *.jpeg *.webp)"
        )
        self._add([Path(n) for n in names])

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Pasta com os capítulos", str(Path.home()))
        if folder:
            self._add([Path(folder)])

    def _add(self, paths: list[Path]) -> None:
        if not paths:
            return
        found, warnings = discover(paths)
        known = {s.origin for s in self.new_sources}
        for source in found:
            if source.origin not in known:
                self.new_sources.append(source)
                self.added.addItem(f"{source.name} ({len(source.files)} págs)")
        if not found:
            QMessageBox.warning(self, APP_DISPLAY_NAME, "Nenhum capítulo encontrado.\n\n" + "\n".join(warnings[:10]))
        self.completeChanged.emit()

    def _remove(self) -> None:
        row = self.added.currentRow()
        if row >= 0:
            self.added.takeItem(row)
            del self.new_sources[row]
            self.completeChanged.emit()

    def isComplete(self) -> bool:
        return bool(self.new_sources or self.checked_existing())


class _TranslationPage(QWizardPage):
    def __init__(self, db: Database, config: Config, work_page: _WorkPage, chapters_page: _ChaptersPage):
        super().__init__()
        self.setTitle("3. Tradução")
        self.setSubTitle("Motor, modelo e como enviar. As falas já traduzidas nunca são enviadas de novo.")
        self._db = db
        self._config = config
        self._work_page = work_page
        self._chapters_page = chapters_page
        self.engine = QComboBox()
        for key in _FLOW_ENGINES:
            self.engine.addItem(ENGINES[key], key)
        current = {"openai-vision": "openai-text", "claude-vision": "claude-text"}.get(config.engine, config.engine)
        self.engine.setCurrentIndex(max(0, self.engine.findData(current)))
        self.openai_model = QComboBox()
        self.openai_model.setEditable(True)
        self.openai_model.addItems(OPENAI_MODELS)
        self.openai_model.setCurrentText(config.openai_model)
        self.claude_model = QComboBox()
        self.claude_model.setEditable(True)
        self.claude_model.addItems(CLAUDE_MODELS)
        self.claude_model.setCurrentText(config.claude_model)
        form = QFormLayout()
        form.addRow("Motor:", self.engine)
        self.openai_label, self.claude_label = QLabel("Modelo:"), QLabel("Modelo:")
        form.addRow(self.openai_label, self.openai_model)
        form.addRow(self.claude_label, self.claude_model)

        self.use_limit = QCheckBox("Pausar e perguntar se o custo estimado passar de")
        self.use_limit.setChecked(True)
        self.limit = QDoubleSpinBox()
        self.limit.setPrefix("US$ ")
        self.limit.setDecimals(2)
        self.limit.setRange(0.0, 10_000.0)
        self.limit.setSingleStep(0.5)
        self.limit.setValue(config.flow_cost_limit)
        limit_row = QHBoxLayout()
        limit_row.addWidget(self.use_limit)
        limit_row.addWidget(self.limit)
        limit_row.addStretch(1)
        self.limit_box = QWidget()
        self.limit_box.setLayout(limit_row)
        self.unread_note = QLabel("")
        self.unread_note.setWordWrap(True)
        self.unread_note.setStyleSheet("color: gray;")

        self._layout = QVBoxLayout(self)
        self._layout.addLayout(form)
        self.widget: TranslationOptions | None = None
        self._holder = QVBoxLayout()
        self._layout.addLayout(self._holder)
        self._layout.addWidget(self.unread_note)
        self._layout.addWidget(self.limit_box)
        self._layout.addStretch(1)
        self.engine.currentIndexChanged.connect(lambda _i: self._rebuild())
        self.openai_model.currentTextChanged.connect(lambda _t: self._rebuild())
        self.claude_model.currentTextChanged.connect(lambda _t: self._rebuild())
        self.use_limit.toggled.connect(self.limit.setEnabled)

    def initializePage(self) -> None:
        self._rebuild()

    def _flow_config(self) -> Config:
        return replace(
            self._config,
            engine=self.engine.currentData(),
            openai_model=self.openai_model.currentText().strip() or OPENAI_MODELS[0],
            claude_model=self.claude_model.currentText().strip() or CLAUDE_MODELS[0],
        )

    def _rebuild(self) -> None:
        """As opções dependem do motor (Batch API só na OpenAI, nomes só com IA): recria o painel quando ele muda."""
        engine = self.engine.currentData()
        self.openai_model.setVisible(engine == "openai-text")
        self.openai_label.setVisible(engine == "openai-text")
        self.claude_model.setVisible(engine == "claude-text")
        self.claude_label.setVisible(engine == "claude-text")
        self.limit_box.setVisible(engine in LLM_ENGINES)
        config = self._flow_config()
        if self.widget is not None:
            # As escolhas feitas até aqui continuam valendo com o motor novo (o que ele não aceita, ele desliga)
            config = replace(config, **self.widget.remember())
            self._holder.removeWidget(self.widget)
            self.widget.deleteLater()
        work = self._work_page.preview()
        self.widget = TranslationOptions(self._db, work, config, self._chapters_page.checked_existing)
        self._holder.addWidget(self.widget)
        self.widget.refresh()
        new = len(self._chapters_page.new_sources)
        self.unread_note.setText(
            f"{new} capítulo(s) novo(s): eles entram no envio depois de lidos, e o custo deles só é conhecido aí. "
            "A estimativa acima vale só para os capítulos já importados."
            if new
            else ""
        )


class _ResultPage(QWizardPage):
    def __init__(self, config: Config):
        super().__init__()
        self.setTitle("4. Resultado")
        self.setSubTitle("Depois de traduzir, gravar os capítulos com as traduções desenhadas? (opcional)")
        self.generate = QCheckBox("Gerar os capítulos traduzidos no fim")
        self.generate.setChecked(config.flow_generate)
        self.cbz = QRadioButton("CBZ, um arquivo por capítulo (recomendado: abre direto no Mihon, Tachiyomi, Perfect Viewer…)")
        self.folder_format = QRadioButton("Pasta de imagens, uma pasta por capítulo")
        (self.folder_format if config.generate_format == "pasta" else self.cbz).setChecked(True)
        self.folder = QLineEdit(config.generate_dir or str(Path.home()))
        browse = QPushButton("Escolher…")
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Salvar em:"))
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        self.inpaint = QCheckBox("Reconstruir o desenho por baixo do texto fora dos balões (IA local; ~200 MB na primeira vez)")
        self.inpaint.setChecked(config.generate_inpaint)
        self._details = QWidget()
        details = QVBoxLayout(self._details)
        details.setContentsMargins(24, 0, 0, 0)
        for widget in (self.cbz, self.folder_format):
            details.addWidget(widget)
        details.addLayout(folder_row)
        details.addWidget(self.inpaint)
        layout = QVBoxLayout(self)
        layout.addWidget(self.generate)
        layout.addWidget(self._details)
        layout.addStretch(1)
        self.generate.toggled.connect(self._details.setEnabled)
        self.generate.toggled.connect(lambda _on: self.completeChanged.emit())
        self.folder.textChanged.connect(lambda _t: self.completeChanged.emit())
        self._details.setEnabled(self.generate.isChecked())

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Onde salvar os capítulos traduzidos", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def isComplete(self) -> bool:
        return not self.generate.isChecked() or bool(self.folder.text().strip())


class _SummaryPage(QWizardPage):
    def __init__(self, wizard: FlowWizard):
        super().__init__()
        self.setTitle("5. Resumo")
        self.setSubTitle("Confira e clique em Começar: as etapas rodam uma atrás da outra, e dá para fechar a janela.")
        self._wizard = wizard
        self.text = QLabel("")
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text)
        layout.addStretch(1)

    def initializePage(self) -> None:
        w = self._wizard
        work = w.work_page.preview()
        new = w.work_page.new.isChecked()
        existing = len(w.chapters_page.checked_existing())
        added = len(w.chapters_page.new_sources)
        t = w.translation_page
        options = t.widget.options()
        engine = t.engine.currentData()
        model = {"openai-text": t.openai_model.currentText(), "claude-text": t.claude_model.currentText()}.get(engine)
        lines = [
            f"<b>Obra:</b> {work.name} ({source_name(work.source_lang)})" + (" — nova" if new else ""),
            f"<b>Capítulos:</b> {existing} já importado(s), {added} novo(s) para importar",
            f"<b>Tradução:</b> {ENGINES[engine]}" + (f" · {model}" if model else ""),
            f"&nbsp;&nbsp;{options.pages_per_block} páginas por pedido · "
            + ("Batch API da OpenAI" if options.mode == "batch" else "envio normal"),
        ]
        if options.mode == "batch":
            if options.memory_chapters:
                block = options.memory_block or options.pages_per_block
                lines.append(f"&nbsp;&nbsp;{options.memory_chapters} capítulo(s) de memória na hora, em pedidos de {block} páginas")
            if options.parts > 1:
                lines.append(f"&nbsp;&nbsp;o resto em {options.parts} partes, com a memória atualizada entre elas")
        if options.survey_names:
            lines.append("&nbsp;&nbsp;nomes levantados antes de cada grupo e salvos direto na lista")
        if engine in LLM_ENGINES:
            limit = f"US$ {t.limit.value():.2f}".replace(".", ",") if t.use_limit.isChecked() else None
            lines.append(f"<b>Limite de custo:</b> {limit + ' (acima disso, o fluxo pausa e pergunta)' if limit else 'nenhum'}")
            if t.widget.cost is not None and existing:
                lines.append(f"&nbsp;&nbsp;estimativa dos já importados: {pricing.format_cost(t.widget.cost.total)}")
        r = w.result_page
        if r.generate.isChecked():
            fmt = "pasta de imagens" if r.folder_format.isChecked() else "CBZ"
            extra = ", reconstruindo o desenho" if r.inpaint.isChecked() else ""
            lines.append(f"<b>Resultado:</b> {fmt} em {r.folder.text().strip()}{extra}")
        else:
            lines.append("<b>Resultado:</b> não gerar arquivos (as traduções ficam salvas para a leitura na tela)")
        self.text.setText("<br>".join(lines))


class FlowWindow(QWidget):
    """Andamento de um fluxo: as etapas, a mensagem atual e os botões para continuar ou cancelar."""

    def __init__(self, db: Database, runner, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(f"Fluxo completo — {APP_DISPLAY_NAME}")
        self.setMinimumWidth(520)
        self._db = db
        self._runner = runner
        self._flow_id: int | None = None
        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.steps = QLabel("")
        self.steps.setTextFormat(Qt.TextFormat.RichText)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.resume = QPushButton("Continuar")
        self.resume.clicked.connect(self._resume)
        self.cancel = QPushButton("Cancelar fluxo")
        self.cancel.clicked.connect(self._cancel)
        close = QPushButton("Fechar")
        close.clicked.connect(self.hide)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        for button in (self.resume, self.cancel, close):
            buttons.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.steps)
        layout.addWidget(self.message)
        layout.addLayout(buttons)
        runner.changed.connect(self._on_changed)

    def show_flow(self, flow_id: int) -> None:
        self._flow_id = flow_id
        self.refresh()
        self.show()
        self.raise_()

    def _on_changed(self, flow_id: int) -> None:
        if flow_id == self._flow_id:
            self.refresh()

    def refresh(self) -> None:
        flow = self._db.flow(self._flow_id) if self._flow_id is not None else None
        if flow is None:
            return
        work = self._db.work(flow.work_id)
        options = FlowOptions.from_json(flow.options)
        self.title.setText(f"<b>{work.name if work else '?'}</b> — {len(options.chapter_ids)} capítulo(s)")
        current = STEPS.index(flow.step)
        rows = []
        for index, step in enumerate(STEPS[:-1]):
            if step == "gerar" and not options.generate:
                continue
            if index < current or flow.state == "concluido":
                mark = "✓"
            elif index == current:
                mark = {"pausado": "⏸", "cancelado": "✖"}.get(flow.state, "▶")
            else:
                mark = "·"
            label = STEP_LABELS[step]
            rows.append(f"{mark} <b>{label}</b>" if index == current and flow.state != "concluido" else f"{mark} {label}")
        self.steps.setText("<br>".join(rows))
        self.message.setText(flow.message or "")
        paused = flow.state == "pausado"
        self.resume.setVisible(paused)
        self.resume.setText("Continuar mesmo assim" if paused and flow.step == "custo" else "Continuar")
        self.cancel.setVisible(flow.state in ("ativo", "pausado"))

    def _resume(self) -> None:
        flow = self._db.flow(self._flow_id)
        if flow is not None:
            self._runner.resume(flow.id, accept_cost=flow.step == "custo")

    def _cancel(self) -> None:
        answer = QMessageBox.question(
            self,
            APP_DISPLAY_NAME,
            "Cancelar o fluxo? O que já foi feito fica salvo (capítulos lidos, traduções). Uma tradução em lote em "
            "andamento continua até você pausá-la ou cancelá-la na janela do lote.",
        )
        if answer == QMessageBox.StandardButton.Yes and self._flow_id is not None:
            self._runner.cancel(self._flow_id)

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
