"""Ícone do app desenhado em código (um balão de fala)."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = float(size)
        bubble = QPainterPath()
        bubble.addEllipse(QRectF(s * 0.04, s * 0.06, s * 0.92, s * 0.72))
        tail = QPainterPath()
        tail.moveTo(QPointF(s * 0.28, s * 0.66))
        tail.lineTo(QPointF(s * 0.14, s * 0.96))
        tail.lineTo(QPointF(s * 0.5, s * 0.74))
        tail.closeSubpath()
        shape = bubble.united(tail)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2f7de1"))
        painter.drawPath(shape)
        font = QFont()
        font.setBold(True)
        font.setPixelSize(max(7, int(s * 0.4)))
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(QRectF(0, s * 0.06, s, s * 0.72), Qt.AlignmentFlag.AlignCenter, "Tr")
        painter.end()
        icon.addPixmap(pixmap)
    return icon
