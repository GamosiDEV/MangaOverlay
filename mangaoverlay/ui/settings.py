"""Janela de configurações."""

from dataclasses import replace

from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFontComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME, credentials
from ..config import CLAUDE_MODELS, ENGINES, OPENAI_MODELS, Config
from ..languages import AUTO, SOURCES, TARGETS, source_name, target_name
from ..platform_info import is_gnome, is_wayland


def _combo(items: list[tuple[str, str]], current: str) -> QComboBox:
    combo = QComboBox()
    for label, data in items:
        combo.addItem(label, data)
    combo.setCurrentIndex(max(0, combo.findData(current)))
    return combo


def _model_combo(models: list[str], current: str) -> QComboBox:
    combo = QComboBox()
    combo.setEditable(True)
    combo.addItems(models)
    combo.setCurrentText(current)
    return combo


def _hotkey_edit(sequence: str) -> QKeySequenceEdit:
    edit = QKeySequenceEdit(QKeySequence(sequence))
    edit.setMaximumSequenceLength(1)
    edit.setClearButtonEnabled(True)
    return edit


class SettingsDialog(QDialog):
    def __init__(self, config: Config, api_keys: dict[str, str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Configurações — {APP_DISPLAY_NAME}")
        self.setMinimumWidth(520)
        self._config = config
        self._key_fields: dict[str, QLineEdit] = {}

        # Tradução
        self.engine = _combo([(label, key) for key, label in ENGINES.items()], config.engine)
        self.source = _combo([(source_name(c), c) for c in [*SOURCES, AUTO]], config.source_lang)
        self.target = _combo([(target_name(c), c) for c in TARGETS], config.target_lang)
        self.include_free = QCheckBox("Traduzir também textos fora dos balões (narração, onomatopeias)")
        self.include_free.setChecked(config.include_free_text)
        self.use_gpu = QCheckBox("Usar a GPU nos modelos locais (detector, OCR e tradução offline)")
        self.use_gpu.setChecked(config.use_gpu)

        translation = QFormLayout()
        translation.addRow("Motor:", self.engine)
        translation.addRow("Idioma de origem:", self.source)
        translation.addRow("Idioma de destino:", self.target)
        translation.addRow("", self.include_free)
        translation.addRow("", self.use_gpu)
        translation_box = QGroupBox("Tradução")
        translation_box.setLayout(translation)

        # OpenAI
        self.openai_model = _model_combo(OPENAI_MODELS, config.openai_model)
        openai_form = QFormLayout()
        openai_form.addRow("Modelo:", self.openai_model)
        openai_form.addRow("Chave da API:", self._key_row(credentials.OPENAI, api_keys, "sk-…"))
        self._openai_box = QGroupBox("OpenAI")
        self._openai_box.setLayout(openai_form)

        # Claude
        self.claude_model = _model_combo(CLAUDE_MODELS, config.claude_model)
        claude_form = QFormLayout()
        claude_form.addRow("Modelo:", self.claude_model)
        claude_form.addRow("Chave da API:", self._key_row(credentials.ANTHROPIC, api_keys, "sk-ant-…"))
        claude_help = QLabel("Crie a chave em <a href=\"https://platform.claude.com\">platform.claude.com</a>.")
        claude_help.setOpenExternalLinks(True)
        claude_form.addRow("", claude_help)
        self._claude_box = QGroupBox("Claude (Anthropic)")
        self._claude_box.setLayout(claude_form)

        # Aparência e atalhos
        self.font_family = QFontComboBox()
        self.font_family.setCurrentFont(QFont(config.font_family) if config.font_family else self.font())
        self.default_font = QCheckBox("Fonte padrão do sistema")
        self.default_font.setChecked(not config.font_family)
        self.default_font.toggled.connect(lambda on: self.font_family.setEnabled(not on))
        self.font_family.setEnabled(bool(config.font_family))
        font_row = QHBoxLayout()
        font_row.addWidget(self.font_family, 1)
        font_row.addWidget(self.default_font)

        self.colorize = QCheckBox("Colorir a página junto com a tradução")
        self.colorize.setChecked(config.colorize)
        self.hotkey_translate = _hotkey_edit(config.hotkey_translate)
        self.hotkey_colorize = _hotkey_edit(config.hotkey_colorize)
        self.hotkey_hide = _hotkey_edit(config.hotkey_hide)
        general = QFormLayout()
        general.addRow("Fonte da tradução:", font_row)
        general.addRow("", self.colorize)
        general.addRow("Atalho para traduzir:", self.hotkey_translate)
        general.addRow("Atalho para colorir:", self.hotkey_colorize)
        general.addRow("Atalho para esconder:", self.hotkey_hide)
        if is_wayland():
            note = "Registrados como atalhos personalizados do GNOME." if is_gnome() else "No Wayland, configure os atalhos no sistema."
            hint = QLabel(note)
            hint.setStyleSheet("color: gray;")
            general.addRow("", hint)
        general_box = QGroupBox("Geral")
        general_box.setLayout(general)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        for widget in (translation_box, self._openai_box, self._claude_box, general_box):
            layout.addWidget(widget)
        layout.addWidget(buttons)

        self.engine.currentIndexChanged.connect(self._update_enabled)
        self._update_enabled()

    def _update_enabled(self) -> None:
        engine = str(self.engine.currentData())
        self._openai_box.setEnabled(engine.startswith("openai"))
        self._claude_box.setEnabled(engine.startswith("claude"))

    def _key_row(self, name: str, api_keys: dict[str, str], placeholder: str) -> QHBoxLayout:
        field = QLineEdit(api_keys.get(name, ""))
        field.setEchoMode(QLineEdit.EchoMode.Password)
        field.setPlaceholderText(f"{placeholder}  (salva no chaveiro do sistema)")
        show = QCheckBox("Mostrar")
        show.toggled.connect(lambda on: field.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        self._key_fields[name] = field
        row = QHBoxLayout()
        row.addWidget(field, 1)
        row.addWidget(show)
        return row

    def result_config(self) -> Config:
        portable = QKeySequence.SequenceFormat.PortableText
        return replace(
            self._config,
            engine=self.engine.currentData(),
            source_lang=self.source.currentData(),
            target_lang=self.target.currentData(),
            include_free_text=self.include_free.isChecked(),
            use_gpu=self.use_gpu.isChecked(),
            openai_model=self.openai_model.currentText().strip() or OPENAI_MODELS[0],
            claude_model=self.claude_model.currentText().strip() or CLAUDE_MODELS[0],
            font_family="" if self.default_font.isChecked() else self.font_family.currentFont().family(),
            colorize=self.colorize.isChecked(),
            hotkey_translate=self.hotkey_translate.keySequence().toString(portable),
            hotkey_colorize=self.hotkey_colorize.keySequence().toString(portable),
            hotkey_hide=self.hotkey_hide.keySequence().toString(portable),
        )

    def result_api_keys(self) -> dict[str, str]:
        return {name: field.text().strip() for name, field in self._key_fields.items()}

