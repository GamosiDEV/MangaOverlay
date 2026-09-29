"""Aplicativo em segundo plano: ícone na bandeja, atalhos, captura, tradução e sobreposição.

Modo atual: sob demanda. O atalho captura a tela, traduz e mostra a sobreposição até o próximo
atalho (traduzir de novo ou esconder).
"""

import ctypes.util
import os
import signal
import sys
from dataclasses import replace
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QActionGroup, QCursor, QGuiApplication
from pathlib import Path

from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMenu, QMessageBox, QSystemTrayIcon

from . import APP_DISPLAY_NAME, APP_NAME, credentials, ipc, screenshot
from .config import ENGINES, Config
from .batch import BatchRunner, batch_key
from .db import Database
from .importer import Importer
from .sources import discover
from .hotkeys import HotkeyManager
from .languages import AUTO, SOURCES, TARGETS, source_name, target_name
from .pipeline import Pipeline
from .platform_info import is_wayland
from .render import base_font
from .ui.batchdialog import BatchDialog, BatchWindow
from .ui.characters import CharactersDialog
from .ui.icon import app_icon
from .ui.memorydialog import MemoryDialog
from .ui.namereview import NameReviewDialog
from .ui.works import NewWorkDialog, WorksDialog
from .ui.importwindow import ImportWindow
from .ui.overlay import OverlayWindow
from .ui.settings import SettingsDialog


def _log(message: str) -> None:
    """Uma linha no stderr (no Windows, sem console, vai para o arquivo de log do app)."""
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", file=sys.stderr, flush=True)


class _JobSignals(QObject):
    done = Signal(object)
    failed = Signal(object)


class MangaOverlayApp(QObject):
    ipc_command = Signal(str)
    status = Signal(str)  # mensagens do processamento (emitidas da thread de trabalho)

    def __init__(self, qapp: QApplication):
        super().__init__()
        self._qapp = qapp
        # Primeiro de tudo: se outra instância já estiver rodando, AlreadyRunning sai daqui antes de
        # carregar qualquer coisa. Comandos que chegarem antes do fim da inicialização ficam na fila do Qt.
        self.ipc_command.connect(self._on_hotkey, Qt.ConnectionType.QueuedConnection)
        self._ipc = ipc.Server(self.ipc_command.emit)
        self._ipc.start()
        self.config = Config.load()
        self.db = Database()
        if self.db.work(self.config.current_work) is None:
            self.config.current_work = None  # obra apagada ou banco novo
        self.pipeline = Pipeline(self.db)
        # Uma tarefa por vez: os modelos na GPU não são usados em paralelo
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._running_jobs: set[_JobSignals] = set()
        self._overlays: dict[str, OverlayWindow] = {}
        self._busy = False
        self._settings_open = False

        self.importer = Importer(self.db, self.pipeline, self)
        self._import_window = ImportWindow()
        self._import_window.setWindowIcon(app_icon())
        self._import_window.stop_requested.connect(self.importer.stop)
        self.importer.progress.connect(self._import_window.update_progress)
        self.importer.finished.connect(self._on_import_finished)

        self.batch_runner = BatchRunner(self.db, self.pipeline, self)
        self._batch_window = BatchWindow()
        self._batch_window.setWindowIcon(app_icon())
        self._batch_window.pause_requested.connect(self._pause_batches)
        # Lotes da Batch API esperam a OpenAI (minutos a horas): confere o andamento a cada minuto
        self._batch_poll = QTimer(self)
        self._batch_poll.setInterval(60_000)
        self._batch_poll.timeout.connect(self._poll_remote_batches)
        self._batch_poll.start()
        self.batch_runner.progress.connect(self._batch_window.update_progress)
        self.batch_runner.log.connect(self._batch_window.add_log)
        self.batch_runner.finished.connect(self._on_batch_finished)

        self._build_tray()
        self.status.connect(lambda message: self._notify(message, 5000))

        self.hotkeys = HotkeyManager(self)
        self.hotkeys.triggered.connect(self._on_hotkey)
        self._apply_hotkeys()

        self._warm_up()
        self._resume_import(silent=True)
        self._resume_batches(silent=True)

    def _warm_up(self) -> None:
        # Na fila de trabalho: um atalho apertado durante o carregamento só espera a vez
        self._run(
            self.pipeline.warm_up,
            replace(self.config),
            on_done=lambda _result: None,
            on_failed=lambda exc: self._notify(f"Não foi possível carregar os modelos: {exc}", 12000, warning=True),
        )

    # --- bandeja --------------------------------------------------------------

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_DISPLAY_NAME)
        menu = QMenu()

        self._translate_action = menu.addAction("", self.translate_screen)
        self._colorize_action = menu.addAction("", self.colorize_screen)
        self._hide_action = menu.addAction("", self.hide_translation)
        self._colorize_toggle = menu.addAction("Colorir junto com a tradução")
        self._colorize_toggle.setCheckable(True)
        self._colorize_toggle.toggled.connect(lambda on: on != self.config.colorize and self._update_config(colorize=on))
        self._saved_only_toggle = menu.addAction("Só traduções salvas (nunca traduzir de novo)")
        self._saved_only_toggle.setCheckable(True)
        self._saved_only_toggle.toggled.connect(lambda on: on != self.config.saved_only and self._update_config(saved_only=on))
        menu.addSeparator()

        self._work_menu = menu.addMenu("")
        self._work_menu.aboutToShow.connect(self._fill_work_menu)
        menu.addAction("Obras e capítulos…", self._open_works)
        self._source_menu = menu.addMenu("")
        self._source_group = self._choice_actions(self._source_menu, [(source_name(c), c) for c in [*SOURCES, AUTO]], "source_lang")
        self._target_menu = menu.addMenu("")
        self._target_group = self._choice_actions(self._target_menu, [(target_name(c), c) for c in TARGETS], "target_lang")
        self._engine_menu = menu.addMenu("")
        self._engine_group = self._choice_actions(self._engine_menu, [(label, key) for key, label in ENGINES.items()], "engine")

        menu.addSeparator()
        self._characters_action = menu.addAction("Personagens da obra…", self._open_characters)
        self._memory_action = menu.addAction("Memória da obra…", self._open_memory)
        self._names_action = menu.addAction("Revisar nomes nas traduções…", self._review_names)
        menu.addAction("Importar capítulos…", self._import_chapters)
        self._batch_action = menu.addAction("Traduzir capítulos…", self._translate_chapters)
        self._batch_resume_action = menu.addAction("", lambda: self._resume_batches(silent=False))
        self._batch_progress_action = menu.addAction("Andamento da tradução em lote…", self._show_batch_window)
        self._resume_action = menu.addAction("", lambda: self._resume_import(silent=False))
        self._import_progress_action = menu.addAction("Ver progresso da importação", self._import_window.show)
        menu.aboutToShow.connect(self._refresh_import_actions)
        menu.addAction("Esquecer as traduções desta obra…", self._forget_translations)
        menu.addAction("Configurações…", self.open_settings)
        menu.addAction("Sair", self.quit)

        self._menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self._refresh_menu()
        self.tray.show()

    def _choice_actions(self, menu: QMenu, choices: list[tuple[str, str]], field: str) -> QActionGroup:
        group = QActionGroup(menu)
        for label, value in choices:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setData(value)
            group.addAction(action)
        group.triggered.connect(lambda a: self._update_config(**{field: a.data()}))
        return group

    def _refresh_menu(self) -> None:
        c = self.config
        hint = lambda key: f"  ({key})" if key else ""  # noqa: E731
        self._translate_action.setText(f"Traduzir a tela{hint(c.hotkey_translate)}")
        self._colorize_action.setText(f"Colorir a tela{hint(c.hotkey_colorize)}")
        self._hide_action.setText(f"Esconder{hint(c.hotkey_hide)}")
        self._colorize_toggle.setChecked(c.colorize)
        self._saved_only_toggle.setChecked(c.saved_only)
        work = self.db.work(c.current_work)
        self._work_menu.setTitle(f"Obra: {work.name if work else 'nenhuma'}")
        # Submenu sempre preenchido: o Qt não abre submenus vazios, então montá-lo só no aboutToShow
        # deixava "Nova obra…" inacessível. Adiado para não apagar itens no meio do clique de um deles.
        QTimer.singleShot(0, self._fill_work_menu)
        self._characters_action.setEnabled(work is not None)
        self._memory_action.setEnabled(work is not None)
        self._names_action.setEnabled(work is not None)
        self._source_menu.setTitle(f"Origem: {source_name(c.source_lang)}")
        self._target_menu.setTitle(f"Destino: {target_name(c.target_lang)}")
        self._engine_menu.setTitle(f"Motor: {ENGINES[c.engine]}")
        for group, value in (
            (self._source_group, c.source_lang),
            (self._target_group, c.target_lang),
            (self._engine_group, c.engine),
        ):
            for action in group.actions():
                action.setChecked(action.data() == value)

    def _refresh_import_actions(self) -> None:
        running = self.importer.running
        pending = 0 if running else len(self.db.pending_pages())
        self._resume_action.setText(f"Retomar importação ({pending} página(s) pendente(s))")
        self._resume_action.setVisible(pending > 0)
        self._import_progress_action.setVisible(running)
        batch_running = self.batch_runner.running
        resumable = 0 if batch_running else len(self._resumable_batches())
        self._batch_resume_action.setText(f"Retomar tradução em lote ({resumable} lote(s))")
        self._batch_resume_action.setVisible(resumable > 0)
        # Também depois de pausar ou terminar (para ver o log) e com lote esperando a OpenAI
        self._batch_progress_action.setVisible(batch_running or self._batch_window.has_log or bool(self.db.batches(("ativo",))))
        self._batch_action.setEnabled(self.config.current_work is not None)

    def _open_characters(self) -> None:
        work = self.db.work(self.config.current_work)
        if work is None:
            return
        dialog = CharactersDialog(self.db, work, replace(self.config))
        dialog.setWindowIcon(app_icon())
        if dialog.exec() == CharactersDialog.DialogCode.Accepted and dialog.renames:
            # Grafia mudou: propõe corrigir as traduções já salvas (grátis, sem retraduzir)
            self._review_names(dialog.renames)

    def _review_names(self, renames: list[tuple[str, str]] | None = None) -> None:
        work = self.db.work(self.config.current_work)
        if work is None:
            return
        dialog = NameReviewDialog(self.db, work, replace(self.config), renames)
        dialog.setWindowIcon(app_icon())
        if dialog.exec() == NameReviewDialog.DialogCode.Accepted and dialog.applied:
            # A leitura guarda traduções na memória da sessão: esvazia para mostrar as corrigidas
            self._pool.start(lambda: self.pipeline.clear_cache())
            self._notify(f"{dialog.applied} tradução(ões) corrigida(s).", 5000)

    def _open_memory(self) -> None:
        work = self.db.work(self.config.current_work)
        if work is not None:
            dialog = MemoryDialog(self.db, work)
            dialog.setWindowIcon(app_icon())
            dialog.exec()

    # --- importação -----------------------------------------------------------

    def _import_chapters(self) -> None:
        if self.importer.running:
            self._import_window.show()
            self._notify("Já há uma importação em andamento; espere terminar para importar mais.", 5000)
            return
        if self.config.current_work is None:
            QMessageBox.information(None, APP_DISPLAY_NAME, "Escolha ou crie primeiro a obra a que os capítulos pertencem.")
            self._new_work()
            if self.config.current_work is None:
                return
        work = self.db.work(self.config.current_work)

        choice = QMessageBox(QMessageBox.Icon.Question, APP_DISPLAY_NAME, f"Importar capítulos para “{work.name}”.\n\nO que você quer escolher?")
        files_button = choice.addButton("Arquivos CBZ/ZIP/PDF…", QMessageBox.ButtonRole.AcceptRole)
        folder_button = choice.addButton("Uma pasta…", QMessageBox.ButtonRole.AcceptRole)
        choice.addButton(QMessageBox.StandardButton.Cancel)
        choice.setInformativeText(
            "Uma pasta de imagens é um capítulo. Uma pasta com subpastas, CBZs ou PDFs vira vários capítulos."
        )
        choice.exec()
        if choice.clickedButton() is files_button:
            names, _ = QFileDialog.getOpenFileNames(
                None, "Capítulos", str(Path.home()), "Capítulos (*.cbz *.zip *.pdf *.png *.jpg *.jpeg *.webp)"
            )
            paths = [Path(n) for n in names]
        elif choice.clickedButton() is folder_button:
            folder = QFileDialog.getExistingDirectory(None, "Pasta com os capítulos", str(Path.home()))
            paths = [Path(folder)] if folder else []
        else:
            return
        if not paths:
            return

        chapters, warnings = discover(paths)
        if not chapters:
            QMessageBox.warning(None, APP_DISPLAY_NAME, "Nenhum capítulo encontrado.\n\n" + "\n".join(warnings[:10]))
            return
        pages = sum(len(c.files) for c in chapters)
        listing = "\n".join(f"• {c.name} ({len(c.files)} págs)" for c in chapters[:15])
        if len(chapters) > 15:
            listing += f"\n… e mais {len(chapters) - 15}"
        notes = ("\n\nIgnorados:\n" + "\n".join(warnings[:5])) if warnings else ""
        answer = QMessageBox.question(
            None,
            APP_DISPLAY_NAME,
            f"{len(chapters)} capítulo(s), {pages} página(s), para “{work.name}” ({source_name(work.source_lang)}):\n\n"
            f"{listing}{notes}\n\nA leitura dos balões roda na sua GPU (sem custo de API). Importar?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        added = skipped = 0
        for chapter in chapters:
            if self.db.add_chapter(work.id, chapter.name, chapter.order, chapter.origin, chapter.files) is None:
                skipped += 1
            else:
                added += 1
        if skipped:
            self._notify(f"{skipped} capítulo(s) já tinham sido importados nesta obra e foram ignorados.", 6000)
        if added:
            self._resume_import(silent=False)

    # --- tradução em lote ------------------------------------------------------

    def _translate_chapters(self) -> None:
        work = self.db.work(self.config.current_work)
        if work is None:
            return
        if self.batch_runner.running:
            self._batch_window.show()
            self._notify("Já há uma tradução em lote em andamento; espere terminar ou pause antes de começar outra.", 5000)
            return
        if not self.db.chapters(work.id):
            QMessageBox.information(
                None, APP_DISPLAY_NAME, "Esta obra ainda não tem capítulos importados. Use “Importar capítulos…” primeiro."
            )
            return
        dialog = BatchDialog(self.db, work, replace(self.config))
        dialog.setWindowIcon(app_icon())
        if dialog.exec() != BatchDialog.DialogCode.Accepted or dialog.estimate is None:
            return
        pages_per_block = dialog.block.value()
        if pages_per_block != self.config.batch_pages_per_block or dialog.mode != self.config.batch_mode:
            self._update_config(batch_pages_per_block=pages_per_block, batch_mode=dialog.mode)
        key = batch_key(self.config, work.id, work.source_lang)
        pages = self.db.chapter_pages(dialog.selected_chapters())
        cost = dialog.estimate.cost
        if dialog.mode == "batch" and cost is not None:
            cost *= 0.5
        self.db.create_batch(work.id, key, pages_per_block, pages, cost, dialog.mode, dialog.sync_pages())
        via = " pela Batch API" if dialog.mode == "batch" else ""
        self._start_batches(f"Traduzindo {dialog.estimate.new_lines} falas de “{work.name}” com {key.model}{via}…")

    def _resumable_batches(self):
        """Só lotes pausados, do mais recente para o mais antigo. Lotes concluídos nunca são reenviados: blocos que
        "falharam" são falas que o modelo não traduz (reticências, onomatopeias), e reenviá-los só gastava."""
        return list(reversed(self.db.batches(("pausado",))))

    def _resume_batches(self, silent: bool) -> None:
        """Ao abrir o app (silent): continua o que estava ativo quando o app fechou. Pelo menu: o lote pausado que o
        usuário escolher (o mais recente já vem selecionado; os outros continuam pausados)."""
        if not silent:
            paused = self._resumable_batches()
            if not paused:
                return
            chosen = paused[0]
            if len(paused) > 1:
                labels = []
                for batch in paused:
                    work = self.db.work(batch.work_id)
                    pending = len(self.db.batch_requests(batch.id))
                    labels.append(f"{work.name if work else '?'} — lote {batch.id} — {pending} bloco(s) pendente(s)")
                label, ok = QInputDialog.getItem(
                    None, APP_DISPLAY_NAME, "Qual tradução em lote retomar? (as outras continuam pausadas)", labels, 0, False
                )
                if not ok:
                    return
                chosen = paused[labels.index(label)]
            self.db.set_batch_state(chosen.id, "ativo")
        active = self.db.batches(("ativo",))
        if not active:
            return
        if silent:
            if self.batch_runner.start(self.config):
                self._notify(f"Retomando a tradução em lote ({len(active)} lote(s)).", 5000)
        else:
            self._start_batches("Retomando a tradução em lote…")

    def _pause_batches(self) -> None:
        """Pausa o envio normal na hora; lotes da Batch API são cancelados na OpenAI na próxima verificação
        (o que já tinha voltado é guardado; o resto fica pendente para quando retomar)."""
        remote = [b for b in self.db.batches(("ativo",)) if b.mode == "batch"]
        for batch in remote:
            self.db.request_pause(batch.id)
        if self.batch_runner.running:
            self.batch_runner.pause()
        elif remote:
            self.batch_runner.start(self.config)

    def _poll_remote_batches(self) -> None:
        if self.batch_runner.running:
            return
        if any(b.mode == "batch" for b in self.db.batches(("ativo",))):
            self.batch_runner.start(self.config)

    def _start_batches(self, title: str) -> None:
        if self.batch_runner.start(self.config):
            self._batch_window.start(title)

    def _show_batch_window(self) -> None:
        self._batch_window.show()
        self._batch_window.raise_()
        self._batch_window.activateWindow()

    def _on_batch_finished(self, outcome) -> None:
        self._batch_window.show_outcome(outcome)
        if not self._batch_window.isVisible():
            if outcome.state == "concluido":
                self._notify("Tradução em lote concluída.", 6000)
            elif outcome.state == "pausado" and outcome.error:
                self._notify(f"Tradução em lote pausada: {outcome.error}", 12000, warning=True)

    def _resume_import(self, silent: bool) -> None:
        """Processa as páginas pendentes. Ao abrir o app (silent), só avisa pela bandeja."""
        pending = len(self.db.pending_pages())
        if pending == 0 or not self.importer.start(self.config):
            return
        if silent:
            self._notify(f"Retomando a importação: {pending} página(s) pendente(s).", 5000)
        else:
            self._import_window.start(pending)

    def _on_import_finished(self, summary) -> None:
        self._import_window.show_summary(summary)
        if not self._import_window.isVisible() and not summary.cancelled:
            errors = f", {summary.errors} com erro" if summary.errors else ""
            self._notify(f"Importação concluída: {summary.pages} página(s), {summary.texts} falas lidas{errors}.", 8000)

    def _fill_work_menu(self) -> None:
        """Lista montada na hora de abrir: sempre reflete o banco."""
        menu = self._work_menu
        menu.clear()
        group = QActionGroup(menu)
        for work in [None, *self.db.works()]:
            action = menu.addAction(work.name if work else "Nenhuma (traduções soltas)")
            action.setCheckable(True)
            action.setChecked((work.id if work else None) == self.config.current_work)
            action.triggered.connect(lambda _checked=False, w=work: self._select_work(w))
            group.addAction(action)
            if work is None:
                menu.addSeparator()
        menu.addSeparator()
        menu.addAction("Nova obra…", self._new_work)

    def _select_work(self, work) -> None:
        if work is None:
            self._update_config(current_work=None)
        else:
            # Cada obra lembra o próprio idioma de origem
            self._update_config(current_work=work.id, source_lang=work.source_lang)

    def _open_works(self) -> None:
        dialog = WorksDialog(
            self.db, self.config.current_work, self.config.source_lang, self._select_work, self._busy_reason,
            lambda: self._resume_import(silent=False),
        )
        dialog.setWindowIcon(app_icon())
        dialog.exec()
        self._refresh_menu()

    def _busy_reason(self) -> str | None:
        """Excluir obras/capítulos no meio de uma importação ou de um lote quebraria o trabalho em andamento."""
        if self.importer.running:
            return "Há uma importação em andamento."
        if self.batch_runner.running or self.db.batches(("ativo",)):
            return "Há uma tradução em lote em andamento."
        return None

    def _new_work(self) -> None:
        dialog = NewWorkDialog(self.config.source_lang)
        dialog.setWindowIcon(app_icon())
        if dialog.exec() != NewWorkDialog.DialogCode.Accepted:
            return
        work = self.db.create_work(dialog.name.text(), dialog.language.currentData())
        self._select_work(work)
        self._notify(f"Lendo agora: {work.name} ({source_name(work.source_lang)}).", 4000)

    def _forget_translations(self) -> None:
        work = self.db.work(self.config.current_work)
        label = f"da obra “{work.name}”" if work else "sem obra"
        count = self.db.count_translations(self.config.current_work)
        answer = QMessageBox.question(
            None,
            APP_DISPLAY_NAME,
            f"Apagar as {count} traduções salvas {label}?\n\nElas serão feitas (e pagas, se o motor for pago) de novo quando você voltar a essas páginas.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # Na fila de trabalho, para não apagar no meio de uma tradução
        work_id = self.config.current_work
        self._pool.start(lambda: self.pipeline.clear_cache(work_id, True))

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.translate_screen()

    def _notify(self, message: str, timeout: int = 8000, warning: bool = False) -> None:
        icon = QSystemTrayIcon.MessageIcon.Warning if warning else QSystemTrayIcon.MessageIcon.Information
        self.tray.showMessage(APP_DISPLAY_NAME, message, icon, timeout)

    # --- configurações --------------------------------------------------------

    def _update_config(self, **changes) -> None:
        if "source_lang" in changes and self.config.current_work is not None and "current_work" not in changes:
            # Mudou o idioma com uma obra aberta: a obra passa a lembrar o novo idioma
            self.db.set_work_language(self.config.current_work, changes["source_lang"])
        self.config = replace(self.config, **changes)
        self.config.save()
        self._refresh_menu()
        self._warm_up()

    def _apply_hotkeys(self) -> None:
        warning = self.hotkeys.apply(self.config.hotkeys)
        if warning:
            self._notify(warning, 15000, warning=True)

    def open_settings(self) -> None:
        if self._settings_open:
            return
        self._settings_open = True
        try:
            api_keys = {name: credentials.get_key(name) for name in (credentials.OPENAI, credentials.ANTHROPIC)}
            dialog = SettingsDialog(self.config, api_keys)
            dialog.setWindowIcon(app_icon())
            if dialog.exec() == SettingsDialog.DialogCode.Accepted:
                self._apply_settings(dialog)
        finally:
            self._settings_open = False

    def _apply_settings(self, dialog: SettingsDialog) -> None:
        new_config = dialog.result_config()
        hotkeys_changed = new_config.hotkeys != self.config.hotkeys
        for name, value in dialog.result_api_keys().items():
            if value == credentials.get_key(name):
                continue
            try:
                credentials.set_key(name, value)
            except Exception as exc:  # erros variados do backend do chaveiro
                QMessageBox.warning(None, APP_DISPLAY_NAME, f"Não foi possível salvar a chave no chaveiro do sistema: {exc}")
                break
        if new_config.source_lang != self.config.source_lang and new_config.current_work is not None:
            self.db.set_work_language(new_config.current_work, new_config.source_lang)
        self.config = new_config
        self.config.save()
        self._refresh_menu()
        self._warm_up()
        if hotkeys_changed:
            self._apply_hotkeys()

    # --- captura e tradução ---------------------------------------------------

    def _run(self, fn, *args, on_done, on_failed) -> None:
        # O pool recebe uma função comum, não uma subclasse de QRunnable: com autoDelete, o Qt apagava o
        # QRunnable na thread de trabalho enquanto o Python ainda tinha o objeto, e o Windows abortava o
        # processo por corrupção de heap (0xC0000374). Os sinais ficam só do lado do Python, na thread principal.
        signals = _JobSignals()
        self._running_jobs.add(signals)

        def finish(handler, value):
            self._running_jobs.discard(signals)
            handler(value)

        def work() -> None:
            try:
                result = fn(*args)
            except Exception as exc:
                signals.failed.emit(exc)
            else:
                signals.done.emit(result)

        signals.done.connect(lambda value: finish(on_done, value))
        signals.failed.connect(lambda exc: finish(on_failed, exc))
        self._pool.start(work)

    def _on_hotkey(self, action: str) -> None:
        if action == "hide":
            self.hide_translation()
        elif action == "translate":
            self.translate_screen()
        elif action == "colorize":
            self.colorize_screen()
        else:  # o app foi aberto de novo enquanto já rodava
            self._notify("Já está rodando no ícone da bandeja.", 4000)

    def _overlay(self, screen_name: str) -> OverlayWindow | None:
        screen = next((s for s in QGuiApplication.screens() if s.name() == screen_name), None)
        if screen is None:
            return None
        overlay = self._overlays.get(screen_name)
        if overlay is None or overlay.screen() is not screen:
            overlay = OverlayWindow(screen)
            self._overlays[screen_name] = overlay
        return overlay

    def hide_translation(self) -> None:
        for overlay in self._overlays.values():
            overlay.clear()

    def translate_screen(self) -> None:
        """Traduz (e colore, se "Colorir junto com a tradução" estiver marcado)."""
        self._start(translate=True, colorize=self.config.colorize)

    def colorize_screen(self) -> None:
        """Só colore; mantém a tradução se ela já estava ligada junto."""
        self._start(translate=self.config.colorize, colorize=True)

    def _start(self, translate: bool, colorize: bool) -> None:
        if self._busy:
            return
        self._busy = True
        self.hide_translation()
        # Espera o compositor tirar a sobreposição da tela antes de capturar
        QTimer.singleShot(250, lambda: self._grab(translate, colorize))

    def _grab(self, translate: bool, colorize: bool) -> None:
        if is_wayland():
            # O portal bloqueia (e pode abrir o diálogo de permissão): roda em segundo plano.
            self._run(
                screenshot.grab_wayland,
                on_done=lambda result: self._process(screenshot.split_screens(*result), translate, colorize),
                on_failed=self._on_failed,
            )
            return
        try:
            if os.environ.get("MO_DBG") == "captura-falsa":  # DEPURAÇÃO (temporário)
                from PIL import Image
                shots = {QGuiApplication.primaryScreen().name(): Image.open(os.environ["MO_DBG_IMG"]).convert("RGB")}
            else:
                shots = screenshot.grab_qt_screens()
        except Exception as exc:
            self._on_failed(exc)
            return
        self._process(shots, translate, colorize)

    def _process(self, shots: dict, translate: bool, colorize: bool) -> None:
        cursor_screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        busy = self._overlay(cursor_screen.name())
        if busy is not None:
            if os.environ.get("MO_DBG") != "sem-sobreposicao":  # DEPURAÇÃO (temporário)
                busy.show_busy()
        config = replace(self.config)

        def work():
            return {
                name: ((self.pipeline.process(image, config, self.status.emit, translate, colorize)
                        if os.environ.get("MO_DBG") != "sem-pipeline" else __import__("mangaoverlay.pipeline").pipeline.ScreenResult([], None, None)),
                       image.size)
                for name, image in shots.items()
            }

        self._run(work, on_done=lambda results: self._on_processed(results, translate, colorize), on_failed=self._on_failed)

    def _on_processed(self, results: dict, translate: bool, colorize: bool) -> None:
        self._busy = False
        self.hide_translation()
        font = base_font(self.config.font_family)
        texts = pages = 0
        for name, (result, size) in results.items():
            overlay = self._overlay(name)
            if overlay is not None and (result.items or result.color_image is not None):
                if os.environ.get("MO_DBG") != "sem-sobreposicao":  # DEPURAÇÃO (temporário)
                    overlay.show_result(result, size, font)
                texts += len(result.items)
                pages += result.color_image is not None
        _log(f"Tela processada: {texts} texto(s), {pages} página(s) colorida(s)")
        if translate and self.config.saved_only:
            found = sum(1 for _r, (result, _s) in results.items() for i in result.items if not i.missing)
            lacking = sum(1 for _r, (result, _s) in results.items() for i in result.items if i.missing)
            if found or lacking:
                extra = f"; {lacking} sem tradução salva (contorno laranja)" if lacking else ""
                self._notify(f"Só traduções salvas: {found} fala(s) do banco{extra}.", 4000)
                return
        if colorize and pages == 0:
            self._notify("Nenhuma página de mangá encontrada na tela para colorir (a página precisa ter balões).", 5000)
        elif translate and not colorize and texts == 0:
            self._notify("Nenhum texto para traduzir encontrado na tela.", 4000)

    def _on_failed(self, exc: Exception) -> None:
        self._busy = False
        self.hide_translation()
        _log(f"Falha ao processar a tela: {exc.__class__.__name__}: {exc}")
        if isinstance(exc, screenshot.CaptureCancelled):
            return
        self._notify(str(exc) or exc.__class__.__name__, 12000, warning=True)

    # --- encerramento ---------------------------------------------------------

    def quit(self) -> None:
        self.hotkeys.stop()
        self._ipc.close()
        self.hide_translation()
        self.tray.hide()
        self.importer.stop()  # o que faltar fica pendente e é retomado na próxima vez
        self.batch_runner.interrupt()  # idem: o lote continua ativo e volta ao abrir o app
        self.importer.wait(10)
        self.batch_runner.wait(10)
        self._pool.waitForDone(5000)  # não fecha o banco no meio de uma gravação
        self.db.close()
        self._qapp.quit()


def prepare_environment() -> str | None:
    """Escolhe o backend gráfico do Qt. Retorna um aviso para o usuário, se houver."""
    # No GNOME/Wayland uma janela não pode pedir "sempre no topo" nem escolher a própria posição.
    # Via XWayland (xcb) os dois pedidos são respeitados. MANGAOVERLAY_NATIVE_WAYLAND=1 desativa.
    if not is_wayland() or os.environ.get("MANGAOVERLAY_NATIVE_WAYLAND") == "1":
        return None
    # O plugin xcb do Qt ≥ 6.5 aborta o processo se faltar a libxcb-cursor.
    if ctypes.util.find_library("xcb-cursor") is None:
        return (
            "A tradução pode não aparecer por cima das outras janelas: falta a biblioteca libxcb-cursor0. "
            "Instale com: sudo apt install libxcb-cursor0 e reabra o app."
        )
    os.environ["QT_QPA_PLATFORM"] = "xcb"
    return None


def run(start_action: str | None = None) -> int:
    environment_warning = prepare_environment()
    qapp = QApplication(sys.argv)
    qapp.setApplicationName(APP_NAME)
    qapp.setApplicationDisplayName(APP_DISPLAY_NAME)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(app_icon())
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C no terminal encerra

    try:
        app = MangaOverlayApp(qapp)
    except ipc.AlreadyRunning:
        # Aberto ao mesmo tempo que outra instância: repassa o pedido para ela e sai
        ipc.send(start_action or "show")
        return 0
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.warning(
            None,
            APP_DISPLAY_NAME,
            "Bandeja do sistema indisponível. No GNOME, ative a extensão 'AppIndicator'. Os atalhos continuam funcionando.",
        )
    if environment_warning:
        app._notify(environment_warning, 15000, warning=True)

    if start_action is not None:
        QTimer.singleShot(300, lambda: app._on_hotkey(start_action))
    else:
        hotkey = f" Use {app.config.hotkey_translate} para traduzir a tela." if app.config.hotkey_translate else ""
        app._notify(f"Rodando em segundo plano.{hotkey}", 4000)
    return qapp.exec()

