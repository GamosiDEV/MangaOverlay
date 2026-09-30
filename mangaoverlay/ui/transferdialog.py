"""Exportar e importar dados: escolha das obras a exportar e o resumo do que uma importação traria."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME
from ..db import Database
from ..transfer import PARTS, Preview

# Nome curto de cada parte, para o nome sugerido do arquivo
_FILE_LABELS = {"obras": "obras", "paginas": "paginas", "traducoes": "traducoes"}


class ExportDialog(QDialog):
    def __init__(self, db: Database, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Exportar dados — {APP_DISPLAY_NAME}")
        self.resize(520, 420)
        intro = QLabel(
            "Gera um arquivo .zip com as partes escolhidas das obras marcadas. As imagens das páginas não vão junto. "
            "O nome e o idioma de cada obra vão sempre, para a importação saber onde colocar o resto."
        )
        intro.setWordWrap(True)
        parts_box = QGroupBox("O que exportar")
        parts_layout = QVBoxLayout(parts_box)
        self.parts: dict[str, QCheckBox] = {}
        for key, label in PARTS.items():
            box = QCheckBox(label)
            box.setChecked(True)
            box.toggled.connect(self._update)
            parts_layout.addWidget(box)
            self.parts[key] = box
        self.works = QListWidget()
        for work in db.works():
            chapters, pages, translations = db.work_stats(work.id)
            item = QListWidgetItem(f"{work.name} — {chapters} capítulo(s), {pages} página(s), {translations} tradução(ões)")
            item.setData(Qt.ItemDataRole.UserRole, work.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.works.addItem(item)
        loose = db.count_translations(None)
        self._loose_count = loose
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
        layout.addWidget(parts_box)
        layout.addWidget(QLabel("Obras:"))
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

    def selected_parts(self) -> tuple[str, ...]:
        return tuple(key for key, box in self.parts.items() if box.isChecked())

    def include_loose(self) -> bool:
        return self.loose.isChecked() and self.loose.isEnabled()

    def file_label(self) -> str:
        """Parte do nome sugerido: "dados" com tudo, senão as partes escolhidas ("obras-traducoes")."""
        parts = self.selected_parts()
        return "dados" if len(parts) == len(PARTS) else "-".join(_FILE_LABELS[p] for p in parts)

    def _update(self) -> None:
        parts = self.selected_parts()
        # As traduções sem obra só fazem sentido exportando traduções
        self.loose.setEnabled(self._loose_count > 0 and "traducoes" in parts)
        self.ok.setEnabled(bool(parts) and (bool(self.selected_works()) or self.include_loose()))


def preview_text(preview: Preview) -> str:
    """Resumo da importação para a pergunta de confirmação (só com as partes que o arquivo tem)."""
    lines = []
    for work in preview.works:
        status = "já existe: recebe só o que faltar" if work.exists else "nova"
        pieces = []
        if "paginas" in preview.parts:
            chapters = f"{work.chapters} capítulo(s)"
            if work.exists and work.new_chapters < work.chapters:
                chapters += f" ({work.chapters - work.new_chapters} já existem e serão pulados)"
            pieces += [chapters, f"{work.pages} página(s)"]
        if "traducoes" in preview.parts:
            pieces.append(f"{work.translations} tradução(ões)")
        if "obras" in preview.parts:
            pieces += [f"{work.characters} personagem(ns)", f"{work.terms} termo(s) do glossário"]
        lines.append(f"• {work.name} ({status}): {', '.join(pieces) or 'só o nome e o idioma'}")
    if preview.loose_translations:
        lines.append(f"• Traduções sem obra: {preview.loose_translations}")
    exported = f" (exportado em {preview.exported_at.replace('T', ' ')})" if preview.exported_at else ""
    contents = ", ".join(PARTS[p].split(" (")[0].lower() for p in preview.parts)
    return (
        f"Importar{exported}.\nO arquivo contém: {contents}.\n\n" + "\n".join(lines or ["(nada)"])
        + "\n\nNada do que você já tem é substituído: traduções, personagens e termos repetidos ficam como estão."
    )
