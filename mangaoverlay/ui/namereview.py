"""Revisão de nomes: mostra cada correção proposta (antes → depois) e só grava as que o usuário deixar marcadas."""

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME, pricing
from ..config import Config
from ..db import Database, Work
from ..names import SurveyError
from ..namefix import Change, all_citing_lines, plan_review, rename_changes, run_review, scan_variations
from ..translators import TranslationError

_REASONS = {"renomeado": "nome mudou na lista", "variação": "grafia parecida (grátis)", "IA": "revisão com IA"}


class _Signals(QObject):
    done = Signal(object, float)  # list[Change], custo real
    failed = Signal(str)


class NameReviewDialog(QDialog):
    def __init__(self, db: Database, work: Work, config: Config, renames: list[tuple[str, str]] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Revisar nomes — {work.name} — {APP_DISPLAY_NAME}")
        self.resize(980, 600)
        self._db = db
        self._work = work
        self._config = config
        self._characters = db.characters(work.id)
        self._changes: list[Change] = []
        self.applied = 0
        self._signals = _Signals()
        self._signals.done.connect(self._on_ai_done)
        self._signals.failed.connect(self._on_ai_failed)

        if renames:
            listing = ", ".join(f"{old} → {new}" for old, new in renames)
            intro_text = f"Nomes alterados na lista ({listing}). Estas são as traduções já salvas que mudam:"
            initial = rename_changes(db, work.id, renames)
            self._ambiguous = []
        else:
            intro_text = (
                "Correções de nomes nas traduções já salvas desta obra, com base na lista de personagens. "
                "Desmarque o que não quiser aplicar."
            )
            scan = scan_variations(db, work.id, self._characters)
            initial = scan.changes
            self._ambiguous = scan.ambiguous
        intro = QLabel(intro_text)
        intro.setWordWrap(True)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "Original", "Antes", "Depois", "Motivo"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        # Revisão com IA: só o que a correção local não resolve (ou a obra inteira, se pedido)
        self.ai_label = QLabel("")
        self.ai_label.setWordWrap(True)
        self.whole = QCheckBox("Revisar a obra inteira (todas as falas que citam personagens da lista)")
        self.whole.toggled.connect(self._update_ai)
        self.ai_button = QPushButton("Revisar com IA")
        self.ai_button.clicked.connect(self._run_ai)
        ai_row = QHBoxLayout()
        ai_row.addWidget(self.ai_label, 1)
        ai_row.addWidget(self.ai_button)
        self.ai_box = QWidget()
        ai_layout = QVBoxLayout(self.ai_box)
        ai_layout.setContentsMargins(0, 0, 0, 0)
        ai_layout.addLayout(ai_row)
        ai_layout.addWidget(self.whole)
        self.ai_box.setVisible(not renames)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.apply = buttons.addButton("Aplicar marcadas", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.ai_box)
        layout.addWidget(self.status)
        layout.addWidget(buttons)
        self._add_changes(initial)  # depois da tela montada (atualiza o status e o botão Aplicar)
        self._update_ai()

    # --- tabela -----------------------------------------------------------------

    def _add_changes(self, changes: list[Change]) -> None:
        known = {c.translation_id for c in self._changes}
        for change in changes:
            if change.translation_id in known:
                continue  # a mesma fala já tem uma correção proposta
            self._changes.append(change)
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row, 0, check)
            for column, value in ((1, change.original), (2, change.before), (3, change.after), (4, _REASONS[change.reason])):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.resizeRowsToContents()
        self._refresh_status()

    def selected(self) -> list[Change]:
        return [c for row, c in enumerate(self._changes) if self.table.item(row, 0).checkState() == Qt.CheckState.Checked]

    def _refresh_status(self) -> None:
        count = len(self._changes)
        self.status.setText(f"{count} correção(ões) proposta(s)." if count else "Nenhuma correção proposta.")
        self.apply.setEnabled(bool(count))

    def accept(self) -> None:
        chosen = self.selected()
        self._db.update_translations([(c.translation_id, c.after) for c in chosen])
        self.applied = len(chosen)
        super().accept()

    # --- revisão com IA ---------------------------------------------------------

    def _ai_lines(self) -> list[tuple[int, str, str]]:
        return all_citing_lines(self._db, self._work.id, self._characters) if self.whole.isChecked() else self._ambiguous

    def _update_ai(self) -> None:
        lines = self._ai_lines()
        if not self._characters:
            self.ai_label.setText("A lista de personagens desta obra está vazia: preencha em “Personagens da obra…”.")
            self.ai_button.setEnabled(False)
            return
        if not lines:
            self.ai_label.setText("Nenhuma fala precisa de revisão com IA: em todas, o nome aparece como na lista.")
            self.ai_button.setEnabled(False)
            return
        try:
            plan = plan_review(self._config, lines)
            cost = pricing.format_cost(plan.estimated_cost)
            self.ai_label.setText(
                f"{len(lines)} fala(s) citam personagens, mas o nome não aparece na tradução (o modelo pode ter usado "
                f"um pronome ou outro nome). Revisar com {plan.model}: custo estimado {cost}."
                if not self.whole.isChecked()
                else f"{len(lines)} fala(s) da obra citam personagens. Revisar com {plan.model}: custo estimado {cost}."
            )
            self.ai_button.setEnabled(True)
        except SurveyError as exc:
            self.ai_label.setText(str(exc))
            self.ai_button.setEnabled(False)

    def _run_ai(self) -> None:
        plan = plan_review(self._config, self._ai_lines())
        self.ai_button.setEnabled(False)
        self.whole.setEnabled(False)
        self.status.setText(f"Revisando {len(plan.lines)} fala(s) com {plan.model}…")
        threading.Thread(target=self._ai_worker, args=(plan,), daemon=True).start()

    def _ai_worker(self, plan) -> None:
        try:
            changes, cost = run_review(plan, self._characters)
        except (TranslationError, SurveyError) as exc:
            self._signals.failed.emit(str(exc))
        except Exception as exc:  # erro inesperado não pode travar a tela
            self._signals.failed.emit(f"{exc.__class__.__name__}: {exc}")
        else:
            self._signals.done.emit(changes, cost)

    def _on_ai_done(self, changes: list[Change], cost: float) -> None:
        before = len(self._changes)
        self._add_changes(changes)
        added = len(self._changes) - before
        self.status.setText(
            f"A IA propôs {added} correção(ões) (custo: {pricing.format_cost(cost)}). {len(self._changes)} no total."
        )
        self.whole.setEnabled(True)

    def _on_ai_failed(self, message: str) -> None:
        self.status.setText(f"<span style='color:#c0392b'>Falha na revisão com IA: {message}</span>")
        self.ai_button.setEnabled(True)
        self.whole.setEnabled(True)

    def reject(self) -> None:
        if self._changes and QMessageBox.question(
            self, APP_DISPLAY_NAME, "Descartar as correções propostas? Nada foi gravado."
        ) != QMessageBox.StandardButton.Yes:
            return
        super().reject()
