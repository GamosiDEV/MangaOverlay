"""Exportar e importar dados: escolha das obras a exportar e o resumo do que uma importação traria."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from .. import APP_DISPLAY_NAME
from ..db import Database
from ..transfer import Preview


class ExportDialog(QDialog):
    def __init__(self, db: Database, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Exportar dados — {APP_DISPLAY_NAME}")
        self.resize(520, 420)
        intro = QLabel(
            "Gera um arquivo .zip com as obras marcadas: capítulos importados (com o texto lido de cada balão), "
            "traduções, personagens e memória da obra. As imagens das páginas não vão junto."
        )
        intro.setWordWrap(True)
        self.works = QListWidget()
        for work in db.works():
            chapters, pages, translations = db.work_stats(work.id)
            item = QListWidgetItem(f"{work.name} — {chapters} capítulo(s), {pages} página(s), {translations} tradução(ões)")
            item.setData(Qt.ItemDataRole.UserRole, work.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.works.addItem(item)
        loose = db.count_translations(None)
        self.loose = QCheckBox(f"Traduções feitas sem obra ({loose})")
        self.loose.setChecked(loose > 0)
        self.loose.setEnabled(loose > 0)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = self.buttons.addButton("Exportar…", QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.works.itemChanged.connect(self._update)
        self.loose.toggled.connect(self._update)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.works, 1)
        layout.addWidget(self.loose)
        layout.addWidget(self.buttons)
        self._update()

    def selected_works(self) -> list[int]:
        return [
            self.works.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.works.count())
            if self.works.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _update(self) -> None:
        self.ok.setEnabled(bool(self.selected_works()) or self.loose.isChecked())


def preview_text(preview: Preview) -> str:
    """Resumo da importação para a pergunta de confirmação."""
    lines = []
    for work in preview.works:
        status = "já existe: recebe só o que faltar" if work.exists else "nova"
        chapters = f"{work.chapters} capítulo(s)"
        if work.exists and work.new_chapters < work.chapters:
            chapters += f" ({work.chapters - work.new_chapters} já existem e serão pulados)"
        lines.append(
            f"• {work.name} ({status}): {chapters}, {work.pages} página(s), {work.translations} tradução(ões), "
            f"{work.characters} personagem(ns), {work.terms} termo(s) do glossário"
        )
    if preview.loose_translations:
        lines.append(f"• Traduções sem obra: {preview.loose_translations}")
    exported = f" (exportado em {preview.exported_at.replace('T', ' ')})" if preview.exported_at else ""
    return (
        f"Importar{exported}:\n\n" + "\n".join(lines or ["(nada)"])
        + "\n\nNada do que você já tem é substituído: traduções, personagens e termos repetidos ficam como estão."
    )
