"""Desenho das traduções: cobre o texto original e encaixa a tradução no balão, no maior tamanho que couber."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen

from .pipeline import OverlayItem

_FLAGS = Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap
_MIN_SIZE = 7


def _rect(box) -> QRectF:
    x0, y0, x1, y1 = box
    return QRectF(x0, y0, x1 - x0, y1 - y0)


def _fit_font(base: QFont, text: str, rect: QRectF) -> QFont:
    """Maior fonte em que o texto quebrado em linhas cabe no retângulo (busca binária)."""
    low, high = _MIN_SIZE, max(_MIN_SIZE, int(min(rect.height(), rect.width() * 0.5)))
    font = QFont(base)
    best = _MIN_SIZE
    while low <= high:
        size = (low + high) // 2
        font.setPixelSize(size)
        bounds = QFontMetricsF(font).boundingRect(rect, _FLAGS, text)
        # Palavras maiores que a largura estouram para os lados em vez de quebrar
        if bounds.height() <= rect.height() and bounds.width() <= rect.width() + 0.5:
            best, low = size, size + 1
        else:
            high = size - 1
    font.setPixelSize(best)
    return font


def base_font(family: str) -> QFont:
    font = QFont(family) if family else QFont()
    if not family:
        font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setBold(True)
    return font


def paint_items(painter: QPainter, items: list[OverlayItem], font: QFont) -> None:
    """Desenha em coordenadas da imagem capturada (o chamador aplica a escala da tela)."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    for item in items:
        if item.missing:
            # Sem tradução salva: o original fica visível, com um contorno tracejado laranja
            pen = QPen(QColor(240, 140, 0, 230), 3, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(_rect(item.fill), 6, 6)
            continue
        background = QColor(*item.background)
        dark = background.lightness() < 110
        foreground = QColor("white") if dark else QColor(20, 20, 20)

        fill = _rect(item.fill)
        radius = min(fill.width(), fill.height()) * 0.2
        cover = QPainterPath()
        cover.addRoundedRect(fill, radius, radius)
        if item.bubble is not None:
            # A caixa do texto vem folgada e às vezes encosta no contorno: limita ao interior do balão
            bubble = _rect(item.bubble)
            inner = QPainterPath()
            inner.addEllipse(bubble.adjusted(*(v * min(bubble.width(), bubble.height()) * 0.04 for v in (1, 1, -1, -1))))
            cover = cover.intersected(inner)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawPath(cover)

        area = _rect(item.area).adjusted(2, 2, -2, -2)
        fitted = _fit_font(font, item.text, area)
        painter.setFont(fitted)
        if item.bubble is None:
            # Fora dos balões o texto pode passar sobre o desenho: halo na cor do fundo para ler melhor
            painter.setPen(background)
            halo = max(1.0, fitted.pixelSize() / 10)
            for dx, dy in ((-1, -1), (-1, 1), (1, -1), (1, 1), (0, -1), (0, 1), (-1, 0), (1, 0)):
                painter.drawText(area.translated(dx * halo, dy * halo), _FLAGS, item.text)
        painter.setPen(foreground)
        painter.drawText(area, _FLAGS, item.text)
