"""Aplicativo em segundo plano: ícone na bandeja, atalhos, captura, tradução e sobreposição.

Modo atual: sob demanda. O atalho captura a tela, traduz e mostra a sobreposição até o próximo
atalho (traduzir de novo ou esconder).
"""

import ctypes.util
import os
import signal
import sys
from dataclasses import replace

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtGui import QActionGroup, QCursor, QGuiApplication
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import APP_DISPLAY_NAME, APP_NAME, credentials, ipc, screenshot
from .config import ENGINES, Config
from .hotkeys import HotkeyManager
from .languages import AUTO, SOURCES, TARGETS, source_name, target_name
from .pipeline import Pipeline
from .platform_info import is_wayland
from .render import base_font
from .ui.icon import app_icon
from .ui.overlay import OverlayWindow
from .ui.settings import SettingsDialog


class _JobSignals(QObject):
    done = Signal(object)
    failed = Signal(object)


class _Job(QRunnable):
    def __init__(self, fn, *args):
        super().__init__()
        self._fn = fn
        self._args = args
        self.signals = _JobSignals()

    def run(self) -> None:
        try:
            result = self._fn(*self._args)
        except Exception as exc:
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(result)


class MangaOverlayApp(QObject):
    ipc_command = Signal(str)
    status = Signal(str)  # mensagens do processamento (emitidas da thread de trabalho)

    def __init__(self, qapp: QApplication):
        super().__init__()
        self._qapp = qapp
        self.config = Config.load()
        self.pipeline = Pipeline()
        # Uma tarefa por vez: os modelos na GPU não são usados em paralelo
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._running_jobs: set[_JobSignals] = set()
        self._overlays: dict[str, OverlayWindow] = {}
        self._busy = False
        self._settings_open = False

        self._build_tray()
        self.status.connect(lambda message: self._notify(message, 5000))

        self.hotkeys = HotkeyManager(self)
        self.hotkeys.triggered.connect(self._on_hotkey)
        self._apply_hotkeys()

        self.ipc_command.connect(self._on_hotkey)
        self._ipc = ipc.Server(self.ipc_command.emit)
        self._ipc.start()
        self._warm_up()

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
        menu.addSeparator()

        self._source_menu = menu.addMenu("")
        self._source_group = self._choice_actions(self._source_menu, [(source_name(c), c) for c in [*SOURCES, AUTO]], "source_lang")
        self._target_menu = menu.addMenu("")
        self._target_group = self._choice_actions(self._target_menu, [(target_name(c), c) for c in TARGETS], "target_lang")
        self._engine_menu = menu.addMenu("")
        self._engine_group = self._choice_actions(self._engine_menu, [(label, key) for key, label in ENGINES.items()], "engine")

        menu.addSeparator()
        menu.addAction("Esquecer traduções guardadas", self._clear_cache)
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

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.translate_screen()

    def _notify(self, message: str, timeout: int = 8000, warning: bool = False) -> None:
        icon = QSystemTrayIcon.MessageIcon.Warning if warning else QSystemTrayIcon.MessageIcon.Information
        self.tray.showMessage(APP_DISPLAY_NAME, message, icon, timeout)

    # --- configurações --------------------------------------------------------

    def _update_config(self, **changes) -> None:
        self.config = replace(self.config, **changes)
        self.config.save()
        self._refresh_menu()
        self._warm_up()

    def _apply_hotkeys(self) -> None:
        warning = self.hotkeys.apply(self.config.hotkeys)
        if warning:
            self._notify(warning, 15000, warning=True)

    def _clear_cache(self) -> None:
        # Roda na fila de trabalho para não mexer no cache durante uma tradução
        self._pool.start(_Job(self.pipeline.clear_cache))

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
        self.config = new_config
        self.config.save()
        self._refresh_menu()
        self._warm_up()
        if hotkeys_changed:
            self._apply_hotkeys()

    # --- captura e tradução ---------------------------------------------------

    def _run(self, fn, *args, on_done, on_failed) -> None:
        job = _Job(fn, *args)
        signals = job.signals
        self._running_jobs.add(signals)

        def finish(handler, value):
            self._running_jobs.discard(signals)
            handler(value)

        signals.done.connect(lambda value: finish(on_done, value))
        signals.failed.connect(lambda exc: finish(on_failed, exc))
        self._pool.start(job)

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
            shots = screenshot.grab_qt_screens()
        except Exception as exc:
            self._on_failed(exc)
            return
        self._process(shots, translate, colorize)

    def _process(self, shots: dict, translate: bool, colorize: bool) -> None:
        cursor_screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        busy = self._overlay(cursor_screen.name())
        if busy is not None:
            busy.show_busy()
        config = replace(self.config)

        def work():
            return {
                name: (self.pipeline.process(image, config, self.status.emit, translate, colorize), image.size)
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
                overlay.show_result(result, size, font)
                texts += len(result.items)
                pages += result.color_image is not None
        if colorize and pages == 0:
            self._notify("Nenhuma página de mangá encontrada na tela para colorir (a página precisa ter balões).", 5000)
        elif translate and not colorize and texts == 0:
            self._notify("Nenhum texto para traduzir encontrado na tela.", 4000)

    def _on_failed(self, exc: Exception) -> None:
        self._busy = False
        self.hide_translation()
        if isinstance(exc, screenshot.CaptureCancelled):
            return
        self._notify(str(exc) or exc.__class__.__name__, 12000, warning=True)

    # --- encerramento ---------------------------------------------------------

    def quit(self) -> None:
        self.hotkeys.stop()
        self._ipc.close()
        self.hide_translation()
        self.tray.hide()
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

    app = MangaOverlayApp(qapp)
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

