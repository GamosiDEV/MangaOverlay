"""Captura de tela: uma imagem por monitor, em pixels físicos, indexada pelo nome do monitor no Qt.

Trabalhar por monitor evita distorções quando os monitores têm escalas diferentes:
o XWayland e o Windows reportam geometrias "virtuais" inconsistentes nesses casos.
"""

from PIL import Image
from PySide6.QtGui import QGuiApplication, QImage

from .platform_info import is_gnome

Rect = tuple[float, float, float, float]  # x, y, largura, altura


class CaptureCancelled(Exception):
    """O usuário negou ou cancelou a captura (ex.: diálogo de permissão do portal)."""


def pil_to_qimage(image: Image.Image) -> QImage:
    rgb = image.convert("RGB")
    data = rgb.tobytes("raw", "RGB")
    return QImage(data, rgb.width, rgb.height, 3 * rgb.width, QImage.Format.Format_RGB888).copy()


def qimage_to_pil(image: QImage) -> Image.Image:
    if image.format() in (QImage.Format.Format_ARGB32, QImage.Format.Format_ARGB32_Premultiplied):
        # Capturas de tela não têm transparência; em telas X11 de 24 bits o byte de alfa
        # pode vir zerado e a conversão direta resultaria numa imagem preta.
        image = QImage(image)
        image.reinterpretAsFormat(QImage.Format.Format_RGB32)
    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    data = bytes(rgb.constBits())
    return Image.frombuffer("RGB", (rgb.width(), rgb.height()), data, "raw", "RGB", rgb.bytesPerLine(), 1).copy()


def grab_qt_screens() -> dict[str, Image.Image]:
    """Windows e X11: o Qt captura cada monitor. Precisa rodar na thread da interface."""
    return {screen.name(): qimage_to_pil(screen.grabWindow(0).toImage()) for screen in QGuiApplication.screens()}


def grab_wayland() -> tuple[Image.Image, dict[str, Rect] | None]:
    """Captura pelo portal (bloqueia; rode fora da thread da interface) e lê o layout dos monitores."""
    from .portal import gnome_monitor_layout, portal_screenshot

    image = portal_screenshot()
    layout = None
    if is_gnome():
        try:
            layout = gnome_monitor_layout()
        except Exception:  # D-Bus indisponível ou formato inesperado: usa a geometria do Qt
            layout = None
    return image, layout


def split_screens(image: Image.Image, layout: dict[str, Rect] | None) -> dict[str, Image.Image]:
    """Recorta a captura da área de trabalho inteira em uma imagem por monitor."""
    screen_names = {screen.name() for screen in QGuiApplication.screens()}
    if not layout or not screen_names & layout.keys():
        layout = {s.name(): tuple(s.geometry().getRect()) for s in QGuiApplication.screens()}

    left = min(x for x, _, _, _ in layout.values())
    top = min(y for _, y, _, _ in layout.values())
    right = max(x + w for x, _, w, _ in layout.values())
    bottom = max(y + h for _, y, _, h in layout.values())
    sx = image.width / (right - left)
    sy = image.height / (bottom - top)
    return {
        name: image.crop(
            (round((x - left) * sx), round((y - top) * sy), round((x + w - left) * sx), round((y + h - top) * sy))
        )
        for name, (x, y, w, h) in layout.items()
    }
