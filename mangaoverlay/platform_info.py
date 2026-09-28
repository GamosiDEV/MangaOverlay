"""Detecção de sistema/sessão gráfica e montagem do comando que abre o app."""

import os
import shlex
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def is_wayland() -> bool:
    if not IS_LINUX:
        return False
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or "WAYLAND_DISPLAY" in os.environ


def is_gnome() -> bool:
    return "gnome" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()


def launch_args(*extra: str) -> list[str]:
    """Argumentos para iniciar o app (executável empacotado ou código-fonte)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *extra]
    python = Path(sys.executable)
    if IS_WINDOWS and (pythonw := python.with_name("pythonw.exe")).exists():
        python = pythonw  # sem janela de console
    return [str(python), str(PROJECT_ROOT / "main.py"), *extra]


def command_line(*extra: str) -> str:
    args = launch_args(*extra)
    return subprocess.list2cmdline(args) if IS_WINDOWS else shlex.join(args)
