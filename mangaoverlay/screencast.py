"""GNOME/Wayland: miniaturas da tela pelo portal ScreenCast (org.freedesktop.portal.ScreenCast) e PipeWire.

Usado só pelo modo em tempo real, para perceber quando a página muda. Na primeira vez, o GNOME pergunta qual tela
compartilhar; a resposta fica guardada (restore_token) e as próximas sessões começam sem perguntar. Enquanto a
transmissão está ligada, o GNOME mostra o ícone de compartilhamento de tela na barra superior; parar por ali desliga
o modo em tempo real.

Os quadros vêm do gst-launch-1.0 (pacotes gstreamer1.0-tools e gstreamer1.0-pipewire), já reduzidos e em cinza,
pelo stdout. O processamento em Python fica mínimo e não há dependência nova no ambiente do app.
"""

import shutil
import subprocess
import threading
import uuid
from pathlib import Path

import numpy as np
from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
from jeepney.io.blocking import open_dbus_connection
from jeepney.wrappers import unwrap_msg

from .realtime import THUMB_WIDTH, Thumbs

_PORTAL = DBusAddress(
    "/org/freedesktop/portal/desktop",
    bus_name="org.freedesktop.portal.Desktop",
    interface="org.freedesktop.portal.ScreenCast",
)
_MONITOR = 1
_CURSOR_HIDDEN = 1
_PERSIST_UNTIL_REVOKED = 2
MISSING_GSTREAMER = (
    "O modo em tempo real no GNOME/Wayland precisa do GStreamer com PipeWire. Instale com: "
    "sudo apt install gstreamer1.0-tools gstreamer1.0-pipewire (ou rode o install.sh de novo)."
)


def available() -> bool:
    return shutil.which("gst-launch-1.0") is not None


class ScreenCastSource:
    """Fonte de miniaturas para o RealtimeWatcher. `latest()` levanta RuntimeError se a transmissão falhou ou acabou."""

    def __init__(self, token_file: Path):
        self._token_file = token_file
        self._lock = threading.Lock()
        self._frames: Thumbs = {}
        self._error: str | None = None
        self._stopped = False
        self._conn = None
        self._procs: list[subprocess.Popen] = []
        threading.Thread(target=self._run, name="screencast", daemon=True).start()

    def latest(self) -> Thumbs | None:
        with self._lock:
            if self._error:
                raise RuntimeError(self._error)
            return dict(self._frames) or None

    def stop(self) -> None:
        self._stopped = True
        for proc in self._procs:
            proc.kill()
        if self._conn is not None:
            try:
                self._conn.close()  # fecha a sessão do portal (o ícone de compartilhamento some)
            except OSError:
                pass

    def _fail(self, message: str) -> None:
        if self._stopped:
            return
        with self._lock:
            self._error = self._error or message

    def _run(self) -> None:
        if not available():
            self._fail(MISSING_GSTREAMER)
            return
        try:
            self._conn = open_dbus_connection(bus="SESSION", enable_fds=True)
            session = self._request("CreateSession", "a{sv}", (), {"session_handle_token": ("s", _token())})["session_handle"][1]
            options = {
                "types": ("u", _MONITOR),
                "multiple": ("b", True),
                "cursor_mode": ("u", _CURSOR_HIDDEN),
                "persist_mode": ("u", _PERSIST_UNTIL_REVOKED),
            }
            restore = self._read_token()
            if restore:
                options["restore_token"] = ("s", restore)
            self._request("SelectSources", "oa{sv}", (session,), options)
            started = self._request("Start", "osa{sv}", (session, ""), {})
            if "restore_token" in started:
                self._save_token(started["restore_token"][1])
            fd = unwrap_msg(self._conn.send_and_get_reply(new_method_call(_PORTAL, "OpenPipeWireRemote", "oa{sv}", (session, {}))))[0]
            raw = fd.to_raw_fd()
        except _Denied:
            self._fail("O compartilhamento da tela foi negado, então o modo em tempo real foi desligado.")
            return
        except Exception as exc:  # portal ausente, D-Bus indisponível, versão sem ScreenCast
            self._fail(f"Não foi possível iniciar a transmissão da tela: {exc}")
            return

        for node, props in started["streams"][1]:
            width, height = props.get("size", ("(ii)", (16, 9)))[1]
            thumb_height = max(1, round(THUMB_WIDTH * height / width))
            pipeline = [
                "gst-launch-1.0", "-q", "pipewiresrc", f"fd={raw}", f"path={node}", "do-timestamp=true", "keepalive-time=500",
                "!", "videorate", "!", "video/x-raw,framerate=2/1", "!", "videoconvert", "!", "videoscale", "!",
                f"video/x-raw,format=GRAY8,width={THUMB_WIDTH},height={thumb_height}", "!", "fdsink", "fd=1", "sync=false",
            ]
            proc = subprocess.Popen(pipeline, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(raw,))
            self._procs.append(proc)
            threading.Thread(
                target=self._read_frames, args=(proc, f"stream-{node}", thumb_height), name=f"screencast-{node}", daemon=True
            ).start()

    def _read_frames(self, proc: subprocess.Popen, name: str, height: int) -> None:
        size = THUMB_WIDTH * height  # GRAY8 com largura múltipla de 4: sem sobra no fim das linhas
        while True:
            data = proc.stdout.read(size)
            if len(data) < size:
                self._fail("A transmissão da tela terminou (compartilhamento encerrado pelo GNOME).")
                return
            frame = np.frombuffer(data, dtype=np.uint8).reshape(height, THUMB_WIDTH)
            with self._lock:
                self._frames[name] = frame

    def _request(self, method: str, signature: str, args: tuple, options: dict) -> dict:
        """Chamada do portal que responde pelo sinal Request.Response (pode abrir o diálogo de compartilhar)."""
        token = _token()
        sender = self._conn.unique_name.lstrip(":").replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response", path=path)
        unwrap_msg(self._conn.send_and_get_reply(message_bus.AddMatch(rule)))
        with self._conn.filter(rule) as queue:
            call = new_method_call(_PORTAL, method, signature, (*args, {"handle_token": ("s", token), **options}))
            unwrap_msg(self._conn.send_and_get_reply(call, timeout=30))
            response, results = self._conn.recv_until_filtered(queue, timeout=300).body
        if response != 0:
            raise _Denied
        return results

    def _read_token(self) -> str:
        try:
            return self._token_file.read_text(encoding="ascii").strip()
        except OSError:
            return ""

    def _save_token(self, token: str) -> None:
        try:
            self._token_file.parent.mkdir(parents=True, exist_ok=True)
            self._token_file.write_text(token, encoding="ascii")
        except OSError:
            pass  # sem o token, o GNOME só pergunta de novo na próxima vez


class _Denied(Exception):
    """O usuário cancelou o diálogo de compartilhar a tela."""


def _token() -> str:
    return f"mangaoverlay_{uuid.uuid4().hex}"
