"""Modo em tempo real: observa a tela e traduz de novo sozinho quando a página muda.

A tela é observada em miniatura (cinza, ~256 px de largura, 2 quadros por segundo), o que é barato. Quando a
miniatura muda em relação à tela traduzida (troca de página, rolagem), a tradução antiga some; quando a tela
para de mudar, o app captura em resolução normal e traduz, como no atalho.

Fontes das miniaturas:
- Windows e X11: captura pelo Qt a cada meio segundo.
- GNOME/Wayland: o portal ScreenCast transmite a tela pelo PipeWire (ver screencast.py). A captura pelo portal
  de screenshot é lenta demais para rodar duas vezes por segundo.
"""

from collections.abc import Callable

import numpy as np
from PIL import Image
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication

THUMB_WIDTH = 256
INTERVAL_MS = 500
# Um pixel da miniatura "mudou" se o tom variou mais que isto (0-255): absorve ruído de compressão e cursor
_PIXEL_DELTA = 12
# A tela mudou se mais que esta fração da miniatura mudou em relação à tela traduzida (o relógio da barra de tarefas
# e o cursor ficam bem abaixo; virar a página ou rolar, bem acima)
_CHANGED = 0.01
# A tela parou se, entre dois quadros seguidos, menos que isto mudou
_STILL = 0.002
# Quadros parados seguidos para considerar que a rolagem ou a animação terminou
_STILL_FRAMES = 2

Thumbs = dict[str, np.ndarray]  # monitor -> miniatura em cinza (uint8)


def thumbnail(image: Image.Image, width: int = THUMB_WIDTH) -> np.ndarray:
    height = max(1, round(image.height * width / image.width))
    return np.asarray(image.convert("L").resize((width, height), Image.Resampling.BILINEAR), dtype=np.uint8)


def _changed_fraction(a: Thumbs, b: Thumbs) -> float:
    """Maior fração de pixels que mudaram entre as miniaturas de um mesmo monitor (1.0 se os monitores mudaram)."""
    if a.keys() != b.keys():
        return 1.0
    worst = 0.0
    for name, frame in a.items():
        other = b[name]
        if frame.shape != other.shape:
            return 1.0
        worst = max(worst, float((np.abs(frame.astype(np.int16) - other) > _PIXEL_DELTA).mean()))
    return worst


class ChangeDetector:
    """Decide, quadro a quadro, quando esconder a tradução (a tela mudou) e quando traduzir de novo (parou).

    Estados: "observando" (há uma tela de referência, a que está traduzida), "mudando" (esperando parar) e
    "ocupado" (o app está traduzindo). No "ocupado", os quadros são comparados com a tela que está sendo traduzida:
    se ela mudar antes de o resultado aparecer (a página nova demorou a abrir), `freeze` avisa e o resultado,
    que é da tela antiga, é descartado.
    """

    def __init__(self) -> None:
        self.state = "observando"
        self._reference: Thumbs | None = None
        self._last: Thumbs | None = None
        self._still = 0
        self._watching_busy = False  # comparando os quadros durante a tradução
        self._dirty = False  # a tela mudou durante a tradução
        self._skip = 0  # quadros a ignorar antes de fixar a tela traduzida (a tradução anterior ainda sumindo)

    def rearm(self) -> None:
        """A tela atual (com a tradução, se houver) vira a referência: o próximo quadro é o novo ponto de partida."""
        self.state = "observando"
        self._reference = None
        self._watching_busy = self._dirty = False

    def busy(self) -> None:
        """Tradução pedida sem um quadro "parado" de referência (atalho, ou o modo acabou de ligar): o primeiro quadro
        durante a tradução é a tela traduzida."""
        self.state = "ocupado"
        self._last = None
        self._watching_busy, self._dirty = True, False
        self._skip = 1  # no X11/Wayland, o primeiro quadro ainda pode ter a tradução anterior na tela

    def freeze(self) -> bool:
        """O resultado vai aparecer: para de comparar (a sobreposição não pode contar como mudança). True se a tela
        mudou durante a tradução, isto é, se o resultado é de uma tela que já não está lá."""
        self._watching_busy = False
        return self._dirty

    def changed_again(self) -> None:
        """Resultado descartado: volta a esperar a tela parar."""
        self.state, self._last, self._still, self._dirty = "mudando", None, 0, False

    def feed(self, frame: Thumbs) -> str:
        """Retorna "nada", "mudou" (esconder a tradução) ou "parou" (traduzir de novo)."""
        if self.state == "ocupado":
            if self._watching_busy:
                if self._skip:
                    self._skip -= 1
                elif self._last is None:
                    self._last = frame
                elif _changed_fraction(frame, self._last) > _CHANGED:
                    self._dirty = True
            return "nada"
        if self.state == "observando":
            if self._reference is None:
                self._reference = frame
                return "nada"
            if _changed_fraction(frame, self._reference) <= _CHANGED:
                return "nada"
            self.state, self._last, self._still = "mudando", frame, 0
            return "mudou"
        # mudando
        if self._last is None:
            self._last = frame
            return "nada"
        if _changed_fraction(frame, self._last) <= _STILL:
            self._still += 1
        else:
            self._still = 0
        self._last = frame
        if self._still >= _STILL_FRAMES:
            # O último quadro é a tela que vai ser traduzida: a comparação durante a tradução parte dele
            self.state = "ocupado"
            self._watching_busy, self._dirty = True, False
            return "parou"
        return "nada"


def grab_qt_thumbnails() -> Thumbs:
    """Windows e X11: miniatura de cada monitor pelo Qt (precisa rodar na thread da interface)."""
    thumbs = {}
    for screen in QGuiApplication.screens():
        image = screen.grabWindow(0).toImage()
        scaled = image.scaledToWidth(THUMB_WIDTH).convertToFormat(image.Format.Format_Grayscale8)
        width, height, stride = scaled.width(), scaled.height(), scaled.bytesPerLine()
        data = np.frombuffer(bytes(scaled.constBits()), dtype=np.uint8).reshape(height, stride)[:, :width]
        thumbs[screen.name()] = data.copy()
    return thumbs


class RealtimeWatcher(QObject):
    """Liga o ChangeDetector a uma fonte de miniaturas e avisa o app."""

    changed = Signal()  # a tela mudou: esconder a tradução
    settled = Signal()  # a tela parou: traduzir de novo
    failed = Signal(str)  # a fonte parou de funcionar (ex.: o usuário negou a transmissão da tela no GNOME)

    def __init__(self, source: Callable[[], Thumbs | None], parent: QObject | None = None, stop_source: Callable[[], None] | None = None):
        super().__init__(parent)
        self._source = source
        self._stop_source = stop_source
        self.detector = ChangeDetector()
        self._timer = QTimer(self)
        self._timer.setInterval(INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    def start(self) -> None:
        self.detector.busy()  # o app traduz a tela assim que o modo liga; o rearm vem depois
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        if self._stop_source is not None:
            self._stop_source()

    def _tick(self) -> None:
        try:
            frame = self._source()
        except Exception as exc:  # captura falhou: desliga em vez de tentar de novo a cada meio segundo
            self.stop()
            self.failed.emit(str(exc) or exc.__class__.__name__)
            return
        if not frame:
            return  # a transmissão ainda não mandou o primeiro quadro
        event = self.detector.feed(frame)
        if event == "mudou":
            self.changed.emit()
        elif event == "parou":
            self.settled.emit()
