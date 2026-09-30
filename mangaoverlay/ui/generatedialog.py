"""Capítulos traduzidos em imagem: escolha dos capítulos, formato, pasta e falas que faltam; janela de progresso."""

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME, pricing
from ..config import ENGINES, Config
from ..db import Database, Work
from ..generate import LLM_ENGINES, GenerateProgress, GenerateSummary, estimate_missing, missing_by_chapter, output_path
from ..languages import target_name


class GenerateDialog(QDialog):
    """`selected`: capítulos que já vêm marcados (os escolhidos na janela de obras); vazio marca todos."""

    def __init__(self, db: Database, work: Work, config: Config, selected: list[int] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Gerar capítulos traduzidos — {work.name} — {APP_DISPLAY_NAME}")
        self.resize(620, 600)
        self._db = db
        self._work = work
        self._config = config
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._missing = missing_by_chapter(db, config, work)
        finally:
            QApplication.restoreOverrideCursor()

        intro = QLabel(
            f"Cada página ganha as traduções salvas em {target_name(config.target_lang)} desenhadas nos balões. "
            "Os arquivos originais não são alterados."
        )
        intro.setWordWrap(True)

        self.chapters = QListWidget()
        for chapter in db.chapters(work.id):
            missing = len(self._missing.get(chapter.id, []))
            status = f"{chapter.pages} págs · {chapter.texts} falas"
            if missing:
                status += f" · {missing} sem tradução"
            elif chapter.texts:
                status += " · tudo traduzido"
            if chapter.read < chapter.pages:
                status += f" · ⚠ {chapter.pages - chapter.read} pág(s) não lidas"
            item = QListWidgetItem(f"{chapter.name}  —  {status}")
            item.setData(Qt.ItemDataRole.UserRole, chapter.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = not selected or chapter.id in selected
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.chapters.addItem(item)
        self.chapters.itemChanged.connect(lambda _item: self._update())
        all_on = QPushButton("Marcar todos")
        all_on.clicked.connect(lambda: self._check_all(True))
        all_off = QPushButton("Desmarcar todos")
        all_off.clicked.connect(lambda: self._check_all(False))
        check_row = QHBoxLayout()
        check_row.addWidget(all_on)
        check_row.addWidget(all_off)
        check_row.addStretch(1)

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

        # Falas sem tradução: sempre oferece traduzir, com o custo antes. Motor pago começa desmarcado.
        self.translate = QCheckBox("")
        self.translate.setChecked(config.engine not in LLM_ENGINES)
        self.missing_note = QLabel("")
        self.missing_note.setWordWrap(True)
        self.missing_note.setStyleSheet("color: gray;")
        self.unread_note = QLabel("")
        self.unread_note.setWordWrap(True)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Gerar")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        note = QLabel(
            "As imagens geradas são para a sua leitura. Se decidir compartilhá-las, a responsabilidade é sua: "
            "a obra continua sendo de quem a publicou."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-size: small;")

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(QLabel("<b>Capítulos</b>"))
        layout.addWidget(self.chapters, 1)
        layout.addLayout(check_row)
        layout.addWidget(QLabel("<b>Formato</b>"))
        layout.addWidget(self.cbz)
        layout.addWidget(self.folder_format)
        layout.addLayout(folder_row)
        layout.addWidget(QLabel("<b>Falas sem tradução salva</b>"))
        layout.addWidget(self.translate)
        layout.addWidget(self.missing_note)
        layout.addWidget(self.unread_note)
        layout.addWidget(note)
        layout.addWidget(self.buttons)
        self._update()

    @property
    def fmt(self) -> str:
        return "pasta" if self.folder_format.isChecked() else "cbz"

    @property
    def translate_missing(self) -> bool:
        # isHidden, e não isVisible: depois que a janela fecha, nada nela está "visível"
        return not self.translate.isHidden() and self.translate.isChecked()

    def selected_chapters(self) -> list[int]:
        return [
            self.chapters.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.chapters.count())
            if self.chapters.item(i).checkState() == Qt.CheckState.Checked
        ]

    def target_folder(self) -> Path:
        return Path(self.folder.text().strip()).expanduser()

    def _check_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.chapters.count()):
            self.chapters.item(i).setCheckState(state)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Onde salvar os capítulos traduzidos", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def _update(self) -> None:
        chosen = self.selected_chapters()
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(chosen))
        missing = estimate_missing(self._db, self._config, self._work, self._missing, chosen)
        engine = ENGINES[self._config.engine]
        if missing.lines:
            self.translate.setText(f"Traduzir as {missing.lines} fala(s) que faltam antes de desenhar")
            self.translate.setVisible(True)
            if self._config.engine in LLM_ENGINES:
                cost = pricing.format_cost(missing.cost)
                self.missing_note.setText(
                    f"Motor atual: {engine}. Custo aproximado: {cost} ({missing.pages} página(s) com falas faltando). "
                    "Sem esta opção, essas falas ficam como no original."
                )
            else:
                self.missing_note.setText(f"Motor atual: {engine}, gratuito. Sem esta opção, essas falas ficam como no original.")
        else:
            self.translate.setVisible(False)
            self.missing_note.setText("Todas as falas lidas dos capítulos escolhidos já têm tradução salva." if chosen else "")
        self.unread_note.setText(
            f"⚠ {missing.unread_pages} página(s) ainda não foram lidas pelo OCR (importação pendente ou com erro): "
            "entram como no original."
            if missing.unread_pages
            else ""
        )
        self.unread_note.setVisible(bool(missing.unread_pages))

    def accept(self) -> None:
        folder = self.target_folder()
        if not self.folder.text().strip():
            self.folder.setFocus()
            return
        chapters = {c.id: c.name for c in self._db.chapters(self._work.id)}
        existing = [
            path.name for path in (output_path(folder, self._work.name, chapters[c], self.fmt) for c in self.selected_chapters())
            if path.exists()
        ]
        if existing:
            names = "\n".join(existing[:5]) + ("\n…" if len(existing) > 5 else "")
            answer = QMessageBox.question(
                self, APP_DISPLAY_NAME, f"{len(existing)} capítulo(s) já foram gerados nesta pasta:\n\n{names}\n\nSubstituir?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        super().accept()


class GenerateWindow(QWidget):
    stop_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(f"Gerando capítulos traduzidos — {APP_DISPLAY_NAME}")
        self.setMinimumWidth(480)
        self._title = QLabel("Preparando…")
        self._title.setWordWrap(True)
        self._bar = QProgressBar()
        self._details = QLabel("")
        self._details.setWordWrap(True)
        self._details.setStyleSheet("color: gray;")
        self._open = QPushButton("Abrir pasta")
        self._open.clicked.connect(self._open_folder)
        self._open.setVisible(False)
        self._button = QPushButton("Parar")
        self._button.clicked.connect(self._on_button)
        self._finished = False
        self._folder: Path | None = None

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self._open)
        buttons.addWidget(self._button)
        layout = QVBoxLayout(self)
        layout.addWidget(self._title)
        layout.addWidget(self._bar)
        layout.addWidget(self._details)
        layout.addLayout(buttons)

    def start(self, chapters: int, folder: Path) -> None:
        self._finished = False
        self._folder = folder
        self._bar.setRange(0, 0)
        self._title.setText(f"Gerando {chapters} capítulo(s) em {folder}…")
        self._details.setText("Pode continuar usando o app; o atalho de traduzir tem prioridade.")
        self._open.setVisible(False)
        self._button.setText("Parar")
        self._button.setEnabled(True)
        self.show()
        self.raise_()

    def update_progress(self, progress: GenerateProgress) -> None:
        self._bar.setRange(0, max(1, progress.total))
        self._bar.setValue(progress.done)
        self._title.setText(f"{progress.chapter} — página {progress.page} ({progress.done + 1} de {progress.total})")
        if progress.seconds_left is not None:
            minutes, seconds = divmod(round(progress.seconds_left), 60)
            self._details.setText(f"Faltam ~{minutes} min {seconds:02d} s" if minutes else f"Faltam ~{seconds} s")

    def show_summary(self, summary: GenerateSummary) -> None:
        self._finished = True
        self._bar.setRange(0, 1)
        self._bar.setValue(1 if not (summary.cancelled or summary.error) else 0)
        files = len(summary.files)
        if summary.error:
            self._title.setText(f"A geração parou: {summary.error}")
        elif summary.cancelled:
            self._title.setText(f"Geração parada. {files} capítulo(s) completo(s) foram gravados.")
        else:
            self._title.setText(f"Pronto: {files} capítulo(s) gerado(s) em {summary.folder}.")
        parts = [f"{summary.pages} página(s)", f"{summary.drawn} fala(s) desenhada(s)"]
        if summary.translated:
            parts.append(f"{summary.translated} traduzida(s) agora")
        if summary.untranslated:
            parts.append(f"{summary.untranslated} ficaram no original (sem tradução)")
        details = " · ".join(parts) + "."
        if summary.error and summary.translated:
            details += " O que já foi traduzido ficou salvo: gerar de novo não paga essas falas outra vez."
        if summary.problems:
            shown = "\n".join(summary.problems[:5]) + ("\n…" if len(summary.problems) > 5 else "")
            details += f"\n\nNão abriram e ficaram de fora:\n{shown}"
        self._details.setText(details)
        self._open.setVisible(bool(summary.files))
        self._button.setText("Fechar")
        self._button.setEnabled(True)

    def _open_folder(self) -> None:
        if self._folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._folder)))

    def _on_button(self) -> None:
        if self._finished:
            self.hide()
            return
        self._button.setEnabled(False)
        self._button.setText("Parando…")
        self.stop_requested.emit()

    def closeEvent(self, event) -> None:
        # Fechar a janela não para a geração: ela continua em segundo plano
        event.ignore()
        self.hide()
