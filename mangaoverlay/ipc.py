"""Comunicação entre processos para manter uma única instância do app.

Usa socket Unix no Linux e named pipe no Windows. Só trafega bytes simples
(sem pickle), então nenhum dado recebido é desserializado como objeto.
"""

import getpass
import os
import sys
import tempfile
import threading
from collections.abc import Callable
from multiprocessing.connection import Client, Listener

from . import APP_ID

COMMANDS = {"translate", "colorize", "hide", "show"}
DEFAULT_NAME = APP_ID


def _address(name: str) -> tuple[str, str]:
    if sys.platform == "win32":
        return rf"\\.\pipe\{name}-{getpass.getuser()}", "AF_PIPE"
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(runtime_dir, f"{name}-{os.getuid()}.sock"), "AF_UNIX"


def send(command: str, name: str = DEFAULT_NAME) -> bool:
    """Envia um comando para a instância em execução. False se não houver nenhuma."""
    address, family = _address(name)
    try:
        with Client(address, family=family) as conn:
            conn.send_bytes(command.encode())
    except (OSError, EOFError):
        return False
    return True


class Server:
    """Escuta comandos de outras execuções do app numa thread em segundo plano."""

    def __init__(self, on_command: Callable[[str], None], name: str = DEFAULT_NAME):
        self._on_command = on_command
        self._name = name
        self._listener: Listener | None = None
        self._closed = False

    def start(self) -> None:
        address, family = _address(self._name)
        if family == "AF_UNIX" and os.path.exists(address):
            # Socket órfão de uma execução que terminou sem limpar (send() já falhou).
            os.unlink(address)
        self._listener = Listener(address, family=family)
        threading.Thread(target=self._serve, name="ipc-server", daemon=True).start()

    def _serve(self) -> None:
        while not self._closed:
            try:
                with self._listener.accept() as conn:
                    command = conn.recv_bytes(64).decode(errors="replace")
            except (OSError, EOFError):
                continue
            if command in COMMANDS:
                self._on_command(command)

    def close(self) -> None:
        self._closed = True
        if self._listener is not None:
            self._listener.close()
