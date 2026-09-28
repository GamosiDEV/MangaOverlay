"""Janela transparente sobre um monitor: mostra as traduções e deixa os cliques passarem para o que está embaixo."""

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QScreen
from PySide6.QtWidgets import QWidget

from ..pipeline import OverlayItem, ScreenResult
from ..render import paint_items
from ..screenshot import pil_to_qimage


class OverlayWindow(QWidget):
    def __init__(self, screen: QScreen):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.X11BypassWindowManagerHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._items: list[OverlayItem] = []
        self._color: tuple[QRectF, QImage] | None = None
        self._image_size = (1, 1)
        self._font = QFont()
        self._busy = False
        self.setScreen(screen)
        self.setGeometry(screen.geometry())

    def show_result(self, result: ScreenResult, image_size: tuple[int, int], font: QFont) -> None:
        self._items = result.items
        self._color = None
        if result.color_image is not None:
            x0, y0, x1, y1 = result.color_box
            self._color = (QRectF(x0, y0, x1 - x0, y1 - y0), pil_to_qimage(result.color_image))
        self._image_size = image_size
        self._font = font
        self._busy = False
        self.setGeometry(self.screen().geometry())
        self.show()
        self.raise_()
        self.update()

    def show_busy(self) -> None:
        """Pontinho discreto no canto enquanto traduz (o resto da tela fica livre)."""
        self._items = []
        self._color = None
        self._busy = True
        self.setGeometry(self.screen().geometry())
        self.show()
        self.raise_()
        self.update()

    def clear(self) -> None:
        self._items = []
        self._color = None
        self._busy = False
        self.hide()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        if self._busy:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(47, 125, 225, 220))
            painter.drawEllipse(QPoint(self.width() - 24, 24), 9, 9)
        if self._items or self._color is not None:
            painter.scale(self.width() / self._image_size[0], self.height() / self._image_size[1])
            if self._color is not None:
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                painter.drawImage(*self._color)  # a página colorida, por baixo das traduções
            paint_items(painter, self._items, self._font)
        painter.end()
