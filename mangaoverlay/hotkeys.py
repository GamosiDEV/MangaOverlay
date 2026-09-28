"""Atalho global de teclado.

- Windows e Linux/X11: pynput escuta o teclado diretamente.
- GNOME no Wayland: apps não podem escutar o teclado, então cada atalho é criado nas
  configurações do GNOME e executa `main.py --translate` (ou `--colorize`, `--hide`), que avisa a instância via IPC.
"""

import ast
import subprocess

from PySide6.QtCore import QObject, Signal

from . import APP_DISPLAY_NAME, APP_ID
from .platform_info import command_line, is_gnome, is_wayland

# Nomes do Qt (QKeySequence.PortableText) -> pynput / GNOME
_PYNPUT_MODIFIERS = {"ctrl": "<ctrl>", "alt": "<alt>", "shift": "<shift>", "meta": "<cmd>"}
_GNOME_MODIFIERS = {"ctrl": "<Control>", "alt": "<Alt>", "shift": "<Shift>", "meta": "<Super>"}
_SPECIAL_KEYS = {
    # qt: (pynput, gnome)
    "space": ("<space>", "space"),
    "print": ("<print_screen>", "Print"),
    "esc": ("<esc>", "Escape"),
    "tab": ("<tab>", "Tab"),
    "return": ("<enter>", "Return"),
    "enter": ("<enter>", "KP_Enter"),
    "ins": ("<insert>", "Insert"),
    "del": ("<delete>", "Delete"),
    "home": ("<home>", "Home"),
    "end": ("<end>", "End"),
    "pgup": ("<page_up>", "Page_Up"),
    "pgdown": ("<page_down>", "Page_Down"),
    "pause": ("<pause>", "Pause"),
}


def _split(sequence: str) -> tuple[list[str], str]:
    parts = sequence.split("+")
    if sequence.endswith("++"):  # a própria tecla "+"
        parts = parts[:-2] + ["+"]
    *modifiers, key = parts
    return [m.strip().lower() for m in modifiers], key.strip()


def to_pynput(sequence: str) -> str:
    modifiers, key = _split(sequence)
    lower = key.lower()
    if lower in _SPECIAL_KEYS:
        key_part = _SPECIAL_KEYS[lower][0]
    elif lower.startswith("f") and lower[1:].isdigit():
        key_part = f"<{lower}>"
    else:
        key_part = lower
    return "+".join([*(_PYNPUT_MODIFIERS[m] for m in modifiers), key_part])


def to_gnome(sequence: str) -> str:
    modifiers, key = _split(sequence)
    lower = key.lower()
    if lower in _SPECIAL_KEYS:
        key_part = _SPECIAL_KEYS[lower][1]
    elif lower.startswith("f") and lower[1:].isdigit():
        key_part = key.upper()
    else:
        key_part = lower
    return "".join(_GNOME_MODIFIERS[m] for m in modifiers) + key_part


def _gvariant_string(value: str) -> str:
    """O gsettings interpreta o valor como texto GVariant; aspas no comando quebrariam o parse."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class GnomeShortcut:
    """Atalho personalizado em Configurações > Teclado > Atalhos personalizados."""

    SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
    BASE_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"

    def __init__(self, action: str, label: str):
        self.path = f"{self.BASE_PATH}{APP_ID}-{action}/"
        self.label = label

    @staticmethod
    def _gsettings(*args: str) -> str:
        result = subprocess.run(["gsettings", *args], check=True, capture_output=True, text=True)
        return result.stdout.strip()

    @classmethod
    def _paths(cls) -> list[str]:
        raw = cls._gsettings("get", cls.SCHEMA, "custom-keybindings")
        return list(ast.literal_eval(raw.removeprefix("@as ")))

    def install(self, binding: str, command: str) -> None:
        paths = self._paths()
        if self.path not in paths:
            self._gsettings("set", self.SCHEMA, "custom-keybindings", str([*paths, self.path]))
        relocatable = f"{self.SCHEMA}.custom-keybinding:{self.path}"
        self._gsettings("set", relocatable, "name", _gvariant_string(self.label))
        self._gsettings("set", relocatable, "command", _gvariant_string(command))
        self._gsettings("set", relocatable, "binding", _gvariant_string(binding))

    def uninstall(self) -> None:
        paths = self._paths()
        if self.path in paths:
            paths.remove(self.path)
            self._gsettings("set", self.SCHEMA, "custom-keybindings", str(paths))


# ação -> (argumento da linha de comando, nome do atalho no GNOME)
ACTIONS = {
    "translate": ("--translate", f"{APP_DISPLAY_NAME}: traduzir a tela"),
    "colorize": ("--colorize", f"{APP_DISPLAY_NAME}: colorir a tela"),
    "hide": ("--hide", f"{APP_DISPLAY_NAME}: esconder a tradução"),
}


class HotkeyManager(QObject):
    triggered = Signal(str)  # nome da ação

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._listener = None

    def apply(self, sequences: dict[str, str]) -> str | None:
        """Registra os atalhos (ação -> sequência do Qt). Retorna uma mensagem para o usuário se algo impedir."""
        self.stop()
        if is_wayland():
            return self._apply_wayland(sequences)

        from pynput import keyboard

        hotkeys = []
        for action, sequence in sequences.items():
            if not sequence:
                continue
            try:
                keys = keyboard.HotKey.parse(to_pynput(sequence))
            except (ValueError, KeyError) as exc:
                return f"Atalho inválido ({sequence}): {exc}"
            hotkeys.append(keyboard.HotKey(keys, lambda a=action: self.triggered.emit(a)))
        if not hotkeys:
            return None

        def on_press(key, injected=False):
            for hotkey in hotkeys:
                hotkey.press(listener.canonical(key))

        def on_release(key, injected=False):
            for hotkey in hotkeys:
                hotkey.release(listener.canonical(key))

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener = listener
        listener.start()
        return None

    def _apply_wayland(self, sequences: dict[str, str]) -> str | None:
        if not is_gnome():
            commands = "\n".join(f"{label}: {command_line(arg)}" for arg, label in ACTIONS.values())
            return f"No Wayland, crie atalhos de teclado nas configurações do sistema com os comandos:\n{commands}"
        for action, (arg, label) in ACTIONS.items():
            shortcut = GnomeShortcut(action, label)
            sequence = sequences.get(action, "")
            try:
                if sequence:
                    shortcut.install(to_gnome(sequence), command_line(arg))
                else:
                    shortcut.uninstall()
            except (OSError, subprocess.CalledProcessError, ValueError, SyntaxError, KeyError) as exc:
                return f"Não foi possível registrar o atalho no GNOME: {exc}"
        return None

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
