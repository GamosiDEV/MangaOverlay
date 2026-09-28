"""Janela de progresso da importação (não bloqueia o resto do app)."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from .. import APP_DISPLAY_NAME
from ..importer import ImportProgress, ImportSummary


def _duration(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes} min {secs:02d} s" if minutes else f"{secs} s"


class ImportWindow(QWidget):
    stop_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(f"Importando capítulos — {APP_DISPLAY_NAME}")
        self.setMinimumWidth(460)
        self._title = QLabel("Preparando…")
        self._title.setWordWrap(True)
        self._bar = QProgressBar()
        self._details = QLabel("")
        self._details.setStyleSheet("color: gray;")
        self._button = QPushButton("Parar")
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

    def start(self, total_pages: int) -> None:
        self._finished = False
        self._bar.setRange(0, max(1, total_pages))
        self._bar.setValue(0)
        self._title.setText(f"Lendo {total_pages} página(s): detecção de balões e OCR na sua GPU.")
        self._details.setText("Pode continuar usando o app; o atalho de traduzir tem prioridade.")
        self._button.setText("Parar")
        self._button.setEnabled(True)
        self.show()
        self.raise_()

    def update_progress(self, progress: ImportProgress) -> None:
        self._bar.setRange(0, max(1, progress.total))
        self._bar.setValue(progress.done)
        self._title.setText(f"{progress.chapter} — página {progress.page} ({progress.done} de {progress.total})")
        parts = [f"{progress.texts} falas lidas"]
        if progress.errors:
            parts.append(f"{progress.errors} página(s) com erro")
        if progress.seconds_left is not None:
            parts.append(f"faltam ~{_duration(progress.seconds_left)}")
        self._details.setText(" · ".join(parts))

    def show_summary(self, summary: ImportSummary) -> None:
        self._finished = True
        if summary.cancelled:
            self._title.setText(f"Importação parada depois de {summary.pages} página(s).")
            self._details.setText("O restante continua pendente: use “Retomar importação” no menu da bandeja.")
        else:
            self._bar.setValue(self._bar.maximum())
            self._title.setText(f"Importação concluída: {summary.pages} página(s), {summary.texts} falas lidas.")
            self._details.setText(
                f"{summary.errors} página(s) com erro (arquivo movido ou corrompido)." if summary.errors else ""
            )
        self._button.setText("Fechar")
        self._button.setEnabled(True)

    def _on_button(self) -> None:
        if self._finished:
            self.hide()
            return
        self._button.setEnabled(False)
        self._button.setText("Parando…")
        self.stop_requested.emit()

    def closeEvent(self, event) -> None:
        # Fechar a janela não para a importação: ela continua em segundo plano
        event.ignore()
        self.hide()
