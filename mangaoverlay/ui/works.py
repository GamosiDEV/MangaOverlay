"""Obras e capítulos: criar, renomear, escolher e excluir obras; ver e excluir capítulos importados.

Janela própria (e não submenu da bandeja): no GNOME os submenus do ícone da bandeja nem sempre abrem.
"""

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
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
from ..db import Database, Work
from ..languages import SOURCES, source_name


class NewWorkDialog(QDialog):
    def __init__(self, source_lang: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Nova obra — {APP_DISPLAY_NAME}")
        self.setMinimumWidth(420)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Nome do mangá, manhwa ou manhua")
        self.language = QComboBox()
        for code in SOURCES:
            self.language.addItem(source_name(code), code)
        self.language.setCurrentIndex(max(0, self.language.findData(source_lang)))
        form = QFormLayout()
        form.addRow("Nome:", self.name)
        form.addRow("Idioma original:", self.language)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def accept(self) -> None:
        if not self.name.text().strip():
            self.name.setFocus()
            return
        super().accept()


class WorksDialog(QDialog):
    """`on_select(work | None)` troca a obra atual; `busy()` diz se há importação/lote rodando (bloqueia exclusões)."""

    def __init__(
        self,
        db: Database,
        current_work: int | None,
        source_lang: str,
        on_select: Callable[[Work | None], None],
        busy: Callable[[], str | None],
        on_reread: Callable[[], None] = lambda: None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Obras e capítulos — {APP_DISPLAY_NAME}")
        self.resize(900, 520)
        self._db = db
        self._current = current_work
        self._source_lang = source_lang
        self._on_select = on_select
        self._busy = busy
        self._on_reread = on_reread

        # Obras
        self.works = QListWidget()
        self.works.currentItemChanged.connect(lambda *_: self._show_chapters())
        self.works.itemDoubleClicked.connect(lambda _item: self._use())
        new = QPushButton("Nova obra…")
        new.clicked.connect(self._new)
        self.use = QPushButton("Ler esta obra")
        self.use.clicked.connect(self._use)
        self.rename = QPushButton("Renomear…")
        self.rename.clicked.connect(self._rename)
        self.delete_work = QPushButton("Excluir obra…")
        self.delete_work.clicked.connect(self._delete_work)
        none = QPushButton("Ler sem obra")
        none.setToolTip("Traduções soltas, sem obra associada")
        none.clicked.connect(lambda: self._select(None))
        work_buttons = QVBoxLayout()
        for button in (new, self.use, self.rename, self.delete_work, none):
            work_buttons.addWidget(button)
        work_buttons.addStretch(1)
        works_row = QHBoxLayout()
        works_row.addWidget(self.works, 1)
        works_row.addLayout(work_buttons)
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Obras</b>"))
        left.addLayout(works_row)

        # Capítulos da obra selecionada
        self.chapters = QTableWidget(0, 4)
        self.chapters.setHorizontalHeaderLabels(["Capítulo", "Páginas", "Lidas", "Falas"])
        self.chapters.verticalHeader().setVisible(False)
        self.chapters.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.chapters.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.chapters.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.chapters.itemSelectionChanged.connect(self._update_buttons)
        self.delete_chapters = QPushButton("Excluir capítulo(s) selecionado(s)…")
        self.delete_chapters.clicked.connect(self._delete_chapters)
        self.reread = QPushButton("Reler capítulo(s) selecionado(s)…")
        self.reread.setToolTip("Lê os balões de novo com a detecção atual; as traduções já feitas continuam salvas")
        self.reread.clicked.connect(self._reread)
        chapter_buttons = QHBoxLayout()
        chapter_buttons.addWidget(self.reread)
        chapter_buttons.addWidget(self.delete_chapters)
        self.summary = QLabel("")
        self.summary.setStyleSheet("color: gray;")
        right = QVBoxLayout()
        right.addWidget(QLabel("<b>Capítulos importados</b>"))
        right.addWidget(self.chapters, 1)
        right.addWidget(self.summary)
        right.addLayout(chapter_buttons)

        body = QHBoxLayout()
        body.addLayout(left, 2)
        body.addLayout(right, 3)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addWidget(close)
        self._load_works(select=current_work)

    # --- obras ------------------------------------------------------------------

    def _load_works(self, select: int | None) -> None:
        self.works.clear()
        for work in self._db.works():
            label = f"{work.name}  ·  {source_name(work.source_lang)}"
            if work.id == self._current:
                label = "▶ " + label + "  (lendo agora)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, work)
            self.works.addItem(item)
            if work.id == select:
                self.works.setCurrentItem(item)
        if self.works.currentItem() is None and self.works.count():
            self.works.setCurrentRow(0)
        self._show_chapters()

    def _selected_work(self) -> Work | None:
        item = self.works.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _select(self, work: Work | None) -> None:
        self._current = work.id if work else None
        self._on_select(work)
        self._load_works(select=work.id if work else None)

    def _new(self) -> None:
        dialog = NewWorkDialog(self._source_lang, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        work = self._db.create_work(dialog.name.text(), dialog.language.currentData())
        self._select(work)  # a obra nova já passa a ser a lida

    def _use(self) -> None:
        work = self._selected_work()
        if work is not None:
            self._select(work)

    def _rename(self) -> None:
        work = self._selected_work()
        if work is None:
            return
        name, ok = QInputDialog.getText(self, APP_DISPLAY_NAME, "Novo nome da obra:", text=work.name)
        if not ok or not name.strip() or name.strip() == work.name:
            return
        if not self._db.rename_work(work.id, name):
            QMessageBox.warning(self, APP_DISPLAY_NAME, f"Já existe uma obra chamada “{name.strip()}”.")
            return
        if work.id == self._current:
            self._on_select(self._db.work(work.id))  # atualiza o nome no menu
        self._load_works(select=work.id)

    def _delete_work(self) -> None:
        work = self._selected_work()
        if work is None or not self._not_busy():
            return
        chapters, pages, translations = self._db.work_stats(work.id)
        answer = QMessageBox.warning(
            self,
            APP_DISPLAY_NAME,
            f"Excluir a obra “{work.name}”?\n\nSerão apagados {chapters} capítulo(s) importado(s) ({pages} páginas), "
            f"{translations} tradução(ões) salva(s), a lista de personagens, a memória e os lotes desta obra.\n\n"
            "Os arquivos originais dos capítulos não são tocados. Não dá para desfazer.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._db.delete_work(work.id)
        if work.id == self._current:
            self._select(None)
        else:
            self._load_works(select=self._current)

    # --- capítulos --------------------------------------------------------------

    def _show_chapters(self) -> None:
        work = self._selected_work()
        self.chapters.setRowCount(0)
        for button in (self.use, self.rename, self.delete_work):
            button.setEnabled(work is not None)
        if work is None:
            self.summary.setText("Crie uma obra com “Nova obra…”.")
            self._update_buttons()
            return
        for chapter in self._db.chapters(work.id):
            row = self.chapters.rowCount()
            self.chapters.insertRow(row)
            name = QTableWidgetItem(chapter.name)
            name.setData(Qt.ItemDataRole.UserRole, chapter.id)
            self.chapters.setItem(row, 0, name)
            for column, value in ((1, chapter.pages), (2, chapter.read), (3, chapter.texts)):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.chapters.setItem(row, column, item)
        chapters, pages, translations = self._db.work_stats(work.id)
        blank = self._db.blank_pages(work.id)
        blank_note = f" {blank} página(s) em branco." if blank else ""
        self.summary.setText(
            f"{chapters} capítulo(s), {pages} página(s), {translations} tradução(ões) salva(s).{blank_note}"
            if chapters
            else "Nenhum capítulo importado. Use “Importar capítulos…” no menu da bandeja."
        )
        self._update_buttons()

    def _selected_chapters(self) -> list[tuple[int, str]]:
        rows = sorted({index.row() for index in self.chapters.selectedIndexes()})
        return [(self.chapters.item(r, 0).data(Qt.ItemDataRole.UserRole), self.chapters.item(r, 0).text()) for r in rows]

    def _update_buttons(self) -> None:
        self.delete_chapters.setEnabled(bool(self._selected_chapters()))
        self.reread.setEnabled(bool(self._selected_chapters()))

    def _delete_chapters(self) -> None:
        chosen = self._selected_chapters()
        if not chosen or not self._not_busy():
            return
        names = ", ".join(name for _i, name in chosen[:5]) + ("…" if len(chosen) > 5 else "")
        answer = QMessageBox.question(
            self,
            APP_DISPLAY_NAME,
            f"Excluir {len(chosen)} capítulo(s) ({names})?\n\nSaem da obra as páginas e o texto lido deles. As traduções "
            "já feitas continuam salvas (servem para a leitura pela tela) e os arquivos originais não são tocados.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._db.delete_chapters([chapter_id for chapter_id, _name in chosen])
        self._show_chapters()

    def _reread(self) -> None:
        chosen = self._selected_chapters()
        if not chosen or not self._not_busy():
            return
        answer = QMessageBox.question(
            self,
            APP_DISPLAY_NAME,
            f"Ler de novo {len(chosen)} capítulo(s)? Os balões são detectados e lidos outra vez (na sua GPU, sem custo). "
            "As traduções já feitas continuam salvas; falas novas encontradas podem ser traduzidas depois em "
            "“Traduzir capítulos…”, e só elas vão para a API.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._db.reset_chapters([chapter_id for chapter_id, _name in chosen])
        self._on_reread()
        self._show_chapters()

    def _not_busy(self) -> bool:
        reason = self._busy()
        if reason:
            QMessageBox.information(self, APP_DISPLAY_NAME, f"{reason} Espere terminar (ou pause) antes de excluir.")
            return False
        return True
