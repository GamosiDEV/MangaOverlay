"""Memória da obra: ver o resumo e o glossário usados nas traduções, e apagar se algo estiver errado."""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME
from ..db import Database, Work


class MemoryDialog(QDialog):
    def __init__(self, db: Database, work: Work, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Memória — {work.name} — {APP_DISPLAY_NAME}")
        self.resize(640, 560)
        self._db = db
        self._work = work
        memory = db.memory(work.id)

        intro = QLabel(
            "O que a IA sabe desta obra ao traduzir: o resumo da história (atualizado a cada capítulo traduzido em "
            "lote) e o glossário de termos, que ela mesma vai montando. Termos novos entram na memória no fim de "
            "cada capítulo, ou a cada 15 termos na leitura pela tela."
        )
        intro.setWordWrap(True)
        summary = QPlainTextEdit(memory.summary or "(ainda sem resumo)")
        summary.setReadOnly(True)
        summary.setMaximumHeight(140)

        table = QTableWidget(len(memory.glossary), 3)
        table.setHorizontalHeaderLabels(["Original", "Tradução", "O que é"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for row, term in enumerate(memory.glossary):
            for column, value in enumerate((term.original, term.translation, term.note)):
                table.setItem(row, column, QTableWidgetItem(value))
        pending = db.pending_terms(work.id)

        forget = QPushButton("Apagar a memória desta obra…")
        forget.clicked.connect(self._forget)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.addButton(forget, QDialogButtonBox.ButtonRole.ResetRole)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(QLabel("<b>Resumo da história</b>"))
        layout.addWidget(summary)
        layout.addWidget(QLabel(f"<b>Glossário</b> ({len(memory.glossary)} termos na memória, {pending} aguardando)"))
        layout.addWidget(table, 1)
        layout.addWidget(buttons)

    def _forget(self) -> None:
        answer = QMessageBox.question(
            self,
            APP_DISPLAY_NAME,
            "Apagar o resumo e o glossário desta obra? As traduções salvas e a lista de personagens não são afetadas; "
            "a memória volta a ser montada nas próximas traduções.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._db.forget_memory(self._work.id)
            self.accept()
