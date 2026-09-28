"""Tela da lista de personagens de uma obra: edição manual, sugestões do texto e levantamento com IA."""

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME
from ..config import Config
from ..db import Character, Database, Work
from ..names import SurveyError, SurveyPlan, plan_survey, run_survey, suggest_names
from ..translators import TranslationError

_GENDERS = ["", "masculino", "feminino", "outro"]
_COLUMNS = ["Nome na tradução *", "Nome original", "Gênero", "Jeito de falar", "Notas"]
_NEW_ROW = QColor(255, 244, 196)  # sugestões do levantamento ainda não revisadas


class _SurveySignals(QObject):
    done = Signal(object)  # list[Character]
    failed = Signal(str)


class CharactersDialog(QDialog):
    def __init__(self, db: Database, work: Work, config: Config, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Personagens — {work.name} — {APP_DISPLAY_NAME}")
        self.resize(980, 560)
        self._db = db
        self._work = work
        self._config = config
        self._signals = _SurveySignals()
        self._signals.done.connect(self._on_survey_done)
        self._signals.failed.connect(self._on_survey_failed)
        # Capítulos analisados por levantamentos desta sessão: marcados só ao salvar
        self._surveyed: list[int] = []

        intro = QLabel(
            "Os nomes daqui vão junto em toda tradução com IA desta obra, para que cada personagem tenha sempre o "
            "mesmo nome e gênero. Só o <b>nome na tradução</b> é obrigatório; o original ajuda quando o nome pode "
            "ser lido de mais de um jeito."
        )
        intro.setWordWrap(True)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        for column in (0, 1, 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(110)
        for character in db.characters(work.id):
            self._add_row(character, saved=True)
        # Trocas de grafia feitas nesta sessão (nome antigo, nome novo): propostas para as traduções salvas
        self.renames: list[tuple[str, str]] = []

        add = QPushButton("Adicionar")
        add.clicked.connect(lambda: self._add_row(Character(name=""), edit=True))
        remove = QPushButton("Remover")
        remove.clicked.connect(self._remove_rows)
        self.quick = QPushButton("Levantamento rápido")
        self.quick.setToolTip(
            f"A IA lê os {config.names_quick_chapters} primeiros capítulos importados ainda não analisados e sugere os "
            "personagens. Usa o modelo principal. Custo baixo."
        )
        self.quick.clicked.connect(lambda: self._survey(full=False))
        self.full = QPushButton("Levantamento completo…")
        self.full.setToolTip("A IA lê todos os capítulos importados ainda não analisados, com um modelo barato.")
        self.full.clicked.connect(lambda: self._survey(full=True))
        table_buttons = QHBoxLayout()
        for button in (add, remove):
            table_buttons.addWidget(button)
        table_buttons.addStretch(1)
        table_buttons.addWidget(self.quick)
        table_buttons.addWidget(self.full)

        self.status = QLabel("")
        self.status.setWordWrap(True)

        left = QVBoxLayout()
        left.addWidget(self.table, 1)
        left.addLayout(table_buttons)
        left.addWidget(self.status)

        # Sugestões locais (grátis): nomes que aparecem no texto já lido
        self.suggestions = QListWidget()
        self.suggestions.itemDoubleClicked.connect(self._add_suggestion)
        found = suggest_names(db.read_texts(work.id), work.source_lang)
        for name, count in found:
            item = QListWidgetItem(f"{name}  ({count}×)")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.suggestions.addItem(item)
        hint = QLabel(
            "Duplo clique adiciona à lista; depois é só escrever o nome na tradução."
            if found
            else "Nenhum nome encontrado ainda. Importe capítulos ou traduza páginas desta obra para ver sugestões."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        right = QVBoxLayout()
        right.addWidget(QLabel("<b>Encontrados no texto</b>"))
        right.addWidget(self.suggestions, 1)
        right.addWidget(hint)
        right_widget = QWidget()
        right_widget.setLayout(right)
        right_widget.setFixedWidth(230)

        body = QHBoxLayout()
        body.addLayout(left, 1)
        body.addWidget(right_widget)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(body, 1)
        layout.addWidget(buttons)

    # --- tabela -----------------------------------------------------------------

    def _add_row(self, character: Character, edit: bool = False, highlight: bool = False, saved: bool = False) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, value in ((0, character.name), (1, character.original), (3, character.speech), (4, character.notes)):
            item = QTableWidgetItem(value)
            if highlight:
                item.setBackground(_NEW_ROW)
            self.table.setItem(row, column, item)
        if saved:
            # Nome como estava salvo: se mudar, as traduções já feitas podem ser corrigidas
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, character.name)
        gender = QComboBox()
        gender.addItems(["—" if g == "" else g for g in _GENDERS])
        gender.setCurrentIndex(_GENDERS.index(character.gender) if character.gender in _GENDERS else 0)
        self.table.setCellWidget(row, 2, gender)
        if edit:
            self.table.setCurrentCell(row, 0)
            self.table.editItem(self.table.item(row, 0))

    def _remove_rows(self) -> None:
        for row in sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)

    def _add_suggestion(self, item: QListWidgetItem) -> None:
        original = item.data(Qt.ItemDataRole.UserRole)
        if any((self.table.item(r, 1) and self.table.item(r, 1).text() == original) for r in range(self.table.rowCount())):
            self.status.setText(f"{original} já está na lista.")
            return
        self._add_row(Character(name="", original=original), edit=True)

    def characters(self) -> list[Character]:
        result = []
        for row in range(self.table.rowCount()):
            text = lambda column: (self.table.item(row, column).text() if self.table.item(row, column) else "").strip()  # noqa: E731
            gender = _GENDERS[self.table.cellWidget(row, 2).currentIndex()]
            result.append(Character(name=text(0), original=text(1), gender=gender, speech=text(3), notes=text(4)))
        return result

    def accept(self) -> None:
        characters = self.characters()
        unnamed = [c.original for c in characters if not c.name and c.original]
        if unnamed:
            QMessageBox.warning(
                self, APP_DISPLAY_NAME, "Falta o nome na tradução de: " + ", ".join(unnamed) + ".\n\nPreencha ou remova essas linhas."
            )
            return
        self.renames = [
            (self.table.item(row, 0).data(Qt.ItemDataRole.UserRole), characters[row].name)
            for row in range(self.table.rowCount())
            if self.table.item(row, 0) is not None
            and self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            and characters[row].name
            and self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) != characters[row].name
        ]
        self._db.save_characters(self._work.id, [c for c in characters if c.name])
        self._db.mark_names_surveyed(self._surveyed)
        super().accept()

    # --- levantamento com IA ----------------------------------------------------

    def _survey(self, full: bool) -> None:
        try:
            plan = plan_survey(self._db, self._work.id, self._config, full)
        except SurveyError as exc:
            QMessageBox.warning(self, APP_DISPLAY_NAME, str(exc))
            return
        if not plan.chapters:
            imported = self._db.chapters(self._work.id)
            message = (
                "Todos os capítulos importados desta obra já foram analisados. Importe capítulos novos para um novo levantamento."
                if imported
                else "O levantamento lê os capítulos importados, e esta obra ainda não tem nenhum. Use “Importar capítulos…” no menu da bandeja."
            )
            QMessageBox.information(self, APP_DISPLAY_NAME, message)
            return
        kind = "completo" if full else "rápido"
        answer = QMessageBox.question(self, APP_DISPLAY_NAME, f"Levantamento {kind}: {plan.description}\n\nContinuar?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True, f"Analisando {len(plan.chapters)} capítulo(s) com {plan.model}…")
        self._running_plan = plan
        threading.Thread(target=self._run_survey, args=(plan,), daemon=True).start()

    def _run_survey(self, plan: SurveyPlan) -> None:
        try:
            found = run_survey(self._db, self._work.id, plan, self._work.source_lang, self._config.target_lang)
        except (SurveyError, TranslationError) as exc:
            self._signals.failed.emit(str(exc))
        except Exception as exc:  # erro inesperado não pode travar a tela
            self._signals.failed.emit(f"{exc.__class__.__name__}: {exc}")
        else:
            self._signals.done.emit(found)

    def _on_survey_done(self, found: list[Character]) -> None:
        self._surveyed += [c.id for c in self._running_plan.chapters]
        in_table = {c.name.casefold() for c in self.characters() if c.name} | {c.original for c in self.characters() if c.original}
        new = [c for c in found if c.name.casefold() not in in_table and (not c.original or c.original not in in_table)]
        for character in new:
            self._add_row(character, highlight=True)
        self._set_busy(
            False,
            f"{len(new)} personagem(ns) novo(s) sugerido(s), destacados em amarelo: revise e clique em Salvar."
            if new
            else "O levantamento não encontrou personagens novos.",
        )

    def _on_survey_failed(self, message: str) -> None:
        self._set_busy(False, f"<span style='color:#c0392b'>Falha no levantamento: {message}</span>")

    def reject(self) -> None:
        if self._surveyed and QMessageBox.question(
            self, APP_DISPLAY_NAME, "Descartar os personagens sugeridos pelo levantamento? Eles não foram salvos."
        ) != QMessageBox.StandardButton.Yes:
            return
        super().reject()

    def _set_busy(self, busy: bool, message: str) -> None:
        self.quick.setEnabled(not busy)
        self.full.setEnabled(not busy)
        self.status.setText(message)
