"""Captura via xdg-desktop-portal (org.freedesktop.portal.Screenshot), usada no Wayland.

Na primeira vez o GNOME pergunta se o app pode capturar a tela; depois lembra.
"""

import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
from jeepney.io.blocking import open_dbus_connection
from jeepney.wrappers import unwrap_msg
from PIL import Image

from .screenshot import CaptureCancelled

_PORTAL = DBusAddress(
    "/org/freedesktop/portal/desktop",
    bus_name="org.freedesktop.portal.Desktop",
    interface="org.freedesktop.portal.Screenshot",
)
_DISPLAY_CONFIG = DBusAddress(
    "/org/gnome/Mutter/DisplayConfig",
    bus_name="org.gnome.Mutter.DisplayConfig",
    interface="org.gnome.Mutter.DisplayConfig",
)


def portal_screenshot(timeout: float = 120) -> Image.Image:
    token = f"mangaoverlay_{uuid.uuid4().hex}"
    conn = open_dbus_connection(bus="SESSION")
    try:
        sender = conn.unique_name.lstrip(":").replace(".", "_")
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        rule = MatchRule(
            type="signal",
            interface="org.freedesktop.portal.Request",
            member="Response",
            path=request_path,
        )
        unwrap_msg(conn.send_and_get_reply(message_bus.AddMatch(rule)))
        with conn.filter(rule) as queue:
            options = {"handle_token": ("s", token), "interactive": ("b", False), "modal": ("b", False)}
            call = new_method_call(_PORTAL, "Screenshot", "sa{sv}", ("", options))
            unwrap_msg(conn.send_and_get_reply(call, timeout=timeout))
            signal = conn.recv_until_filtered(queue, timeout=timeout)
    finally:
        conn.close()

    response, results = signal.body
    if response != 0:
        raise CaptureCancelled("Captura de tela cancelada ou negada pelo sistema.")

    path = Path(unquote(urlparse(results["uri"][1]).path))
    try:
        with Image.open(path) as img:
            image = img.convert("RGB")
    finally:
        # O arquivo foi criado pelo portal só para esta captura.
        path.unlink(missing_ok=True)
    return image


def gnome_monitor_layout() -> dict[str, tuple[float, float, float, float]]:
    """Posição e tamanho lógicos de cada monitor (conector -> x, y, w, h) segundo o Mutter.

    A captura do portal cobre esse layout lógico multiplicado pela maior escala.
    """
    conn = open_dbus_connection(bus="SESSION")
    try:
        _serial, monitors, logical_monitors, properties = unwrap_msg(
            conn.send_and_get_reply(new_method_call(_DISPLAY_CONFIG, "GetCurrentState"))
        )
    finally:
        conn.close()

    physical_layout = properties.get("layout-mode", ("u", 1))[1] == 2
    current_modes = {}
    for (connector, *_), modes, _props in monitors:
        for _id, width, height, *_rest, mode_props in modes:
            if mode_props.get("is-current", ("b", False))[1]:
                current_modes[connector] = (width, height)

    layout = {}
    for x, y, scale, transform, _primary, specs, _props in logical_monitors:
        for connector, *_ in specs:
            if connector not in current_modes:
                continue
            width, height = current_modes[connector]
            if transform % 2:  # 90° e 270° (inclusive espelhados)
                width, height = height, width
            if not physical_layout:
                width, height = width / scale, height / scale
            layout[connector] = (x, y, width, height)
    return layout
