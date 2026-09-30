"""Modo em tempo real: observa a tela e traduz de novo sozinho quando a página muda.

A tela é observada em miniatura (cinza, ~256 px de largura, 2 quadros por segundo), o que é barato. Quando a
miniatura muda em relação à tela traduzida (troca de página, rolagem), a tradução antiga some; quando a tela
para de mudar, o app captura em resolução normal e traduz, como no atalho.

Fontes das miniaturas:
- Windows e X11: captura pelo Qt a cada meio segundo.
- GNOME/Wayland: o portal ScreenCast transmite a tela pelo PipeWire (ver screencast.py). A captura pelo portal
  de screenshot é lenta demais para rodar duas vezes por segundo.
"""

import sys
from collections.abc import Callable
from datetime import datetime

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
# Tentativas quando a tela muda durante a tradução (cada uma exige a tela parada por mais tempo) antes de desistir
# até a próxima mudança: nunca mostra uma tradução de uma tela que já não está lá, e não fica traduzindo sem parar
_MAX_RETRIES = 3

# Quadros (~1 s) depois de a tradução aparecer para conferir que a tela ainda é a traduzida
_CONFIRM_FRAMES = 2
# Quadros (~1 s) a ignorar depois da captura pelo portal de screenshot do GNOME, que dá um flash na tela
PORTAL_FLASH_FRAMES = 2

Thumbs = dict[str, np.ndarray]  # monitor -> miniatura em cinza (uint8)
Masks = dict[str, list[tuple[int, int, int, int]]]  # monitor -> áreas da miniatura cobertas pela tradução


def thumbnail(image: Image.Image, width: int = THUMB_WIDTH) -> np.ndarray:
    height = max(1, round(image.height * width / image.width))
    return np.asarray(image.convert("L").resize((width, height), Image.Resampling.BILINEAR), dtype=np.uint8)


def _changed_fraction(a: Thumbs, b: Thumbs, masks: Masks | None = None) -> float:
    """Maior fração de pixels que mudaram entre as miniaturas de um mesmo monitor (1.0 se os monitores mudaram).
    As áreas em `masks` (onde a tradução está desenhada) ficam fora da conta."""
    if a.keys() != b.keys():
        return 1.0
    worst = 0.0
    for name, frame in a.items():
        other = b[name]
        if frame.shape != other.shape:
            return 1.0
        changed = np.abs(frame.astype(np.int16) - other) > _PIXEL_DELTA
        boxes = (masks or {}).get(name)
        if boxes:
            keep = np.ones(changed.shape, dtype=bool)
            for x0, y0, x1, y1 in boxes:
                keep[max(0, y0) : max(0, y1), max(0, x0) : max(0, x1)] = False
            fraction = float(changed[keep].mean()) if keep.any() else 0.0
        else:
            fraction = float(changed.mean())
        worst = max(worst, fraction)
    return worst


class ChangeDetector:
    """Decide, quadro a quadro, quando esconder a tradução (a tela mudou) e quando traduzir de novo (parou).

    Estados: "observando" (há uma tela de referência, a que está traduzida), "mudando" (esperando parar),
    "ocupado" (o app está traduzindo) e "confirmando" (a tradução acabou de aparecer).

    No "ocupado", os quadros são comparados com a tela que está sendo traduzida: se ela mudar antes de o resultado
    aparecer (a página nova demorou a abrir), `freeze` avisa e o resultado, que é da tela antiga, é descartado.

    Quando o resultado aparece (`displayed`), a referência passa a ser essa mesma tela, sem a sobreposição, e as
    áreas cobertas pela tradução ficam fora da comparação. Assim, uma troca de página logo depois é percebida mesmo
    antes de a sobreposição aparecer nas capturas. (A cor não atrapalha: a colorização mantém o brilho original, e as
    miniaturas são em cinza.) Se a sobreposição mudar as capturas fora dessas áreas (onde o Windows não a exclui das
    capturas, como em máquinas virtuais), a tela não confere duas vezes seguidas e o detector passa a tomar a
    referência depois de a tradução aparecer.
    """

    def __init__(self) -> None:
        self.state = "observando"
        self._reference: Thumbs | None = None
        self._last: Thumbs | None = None
        self._still = 0
        self._watching_busy = False  # comparando os quadros durante a tradução
        self._dirty = False  # a tela mudou durante a tradução
        self._skip = 0  # quadros a ignorar antes de fixar a tela traduzida (a tradução anterior ainda sumindo)
        self._retries = 0  # traduções descartadas seguidas (a tela mudou durante a tradução)
        self._still_needed = _STILL_FRAMES
        self._masks: Masks | None = None
        self._confirm = 0
        self._skip_reference = 0  # quadros a esperar antes de fixar a referência (a tela ainda se acomodando)
        self._suspect = 0  # confirmações seguidas que falharam logo depois de mostrar a tradução
        self.legacy = False  # a sobreposição aparece nas capturas: referência tomada depois de ela aparecer

    def rearm(self) -> None:
        """A tela atual vira a referência, daqui a ~1 s (a tradução escondida ou mostrada ainda está se acomodando)."""
        self.state = "observando"
        self._reference, self._masks = None, None
        self._skip_reference = _CONFIRM_FRAMES
        self._watching_busy = self._dirty = False
        self._retries, self._still_needed = 0, _STILL_FRAMES

    def displayed(self, masks: Masks) -> None:
        """A tradução apareceu na tela. A referência é a tela traduzida (o último quadro antes do resultado)."""
        translated = self._last
        self.rearm()
        if self.legacy or translated is None:
            return
        self.state, self._reference, self._masks, self._confirm = "confirmando", translated, masks, 0

    def busy(self) -> None:
        """Tradução pedida sem um quadro "parado" de referência (atalho, ou o modo acabou de ligar): a tela traduzida
        é o primeiro quadro depois da captura (ver capture_done)."""
        self.state = "ocupado"
        self._last = None
        self._watching_busy, self._dirty = False, False

    def capture_done(self, settle_frames: int = 0) -> None:
        """A tela da tradução foi capturada: a partir daqui, uma mudança invalida o resultado. `settle_frames`: quadros
        a ignorar antes (no GNOME, a captura pelo portal dá um "flash" na tela)."""
        if self.state != "ocupado":
            return
        self._watching_busy, self._skip = True, settle_frames
    def freeze(self) -> bool:
        """O resultado vai aparecer: para de comparar (a sobreposição não pode contar como mudança). True se a tela
        mudou durante a tradução, isto é, se o resultado é de uma tela que já não está lá."""
        self._watching_busy = False
        return self._dirty

    def changed_again(self) -> bool:
        """Resultado descartado: volta a esperar a tela parar, agora por mais tempo (a tela está instável, como uma
        janela abrindo e se ajustando). False depois de _MAX_RETRIES: desistir até a próxima mudança (chamar rearm)."""
        self._retries += 1
        if self._retries > _MAX_RETRIES:
            return False
        self.state, self._last, self._still, self._dirty = "mudando", None, 0, False
        self._still_needed = _STILL_FRAMES * (1 + self._retries)
        return True

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
        if self.state == "confirmando":
            self._confirm += 1
            if self._confirm < _CONFIRM_FRAMES:
                return "nada"
            if _changed_fraction(frame, self._reference, self._masks) <= _CHANGED:
                self.state, self._suspect = "observando", 0
                return "nada"
            # Não confere: a página mudou logo depois da tradução aparecer, ou a sobreposição aparece nas capturas
            self._suspect += 1
            if self._suspect >= 2:
                self.legacy = True
                self.state, self._reference, self._masks = "observando", frame, None
                return "nada"
            self.state, self._last, self._still, self._masks = "mudando", frame, 0, None
            return "mudou"
        if self.state == "observando":
            if self._reference is None:
                if self._skip_reference:
                    self._skip_reference -= 1
                    return "nada"
                self._reference = frame
                return "nada"
            if _changed_fraction(frame, self._reference, self._masks) <= _CHANGED:
                return "nada"
            self.state, self._last, self._still, self._masks = "mudando", frame, 0, None
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
        if self._still >= self._still_needed:
            # O último quadro é a tela que vai ser traduzida: a comparação (depois da captura) parte dele
            self.state = "ocupado"
            self._watching_busy, self._dirty = False, False
            return "parou"
        return "nada"


Areas = dict[str, tuple[float, float, float, float]]  # monitor -> área útil (esquerda, topo, direita, base), em frações


def available_areas() -> Areas:
    """A área de cada monitor sem a barra de tarefas (Windows) ou a barra superior (GNOME)."""
    areas = {}
    for screen in QGuiApplication.screens():
        g, a = screen.geometry(), screen.availableGeometry()
        if g.width() <= 0 or g.height() <= 0:
            continue
        areas[screen.name()] = (
            (a.left() - g.left()) / g.width(),
            (a.top() - g.top()) / g.height(),
            (a.right() + 1 - g.left()) / g.width(),
            (a.bottom() + 1 - g.top()) / g.height(),
        )
    return areas


def only_available(thumbs: Thumbs, areas: Areas) -> Thumbs:
    """Apaga das miniaturas o que fica fora da área útil. No Windows, a barra de tarefas some e volta conforme a
    sobreposição (uma janela do tamanho da tela) aparece, e isso não pode contar como mudança de página. As
    coordenadas não mudam (a tradução continua casando com as áreas das miniaturas)."""
    result = {}
    for name, frame in thumbs.items():
        area = areas.get(name)
        if area is None:
            result[name] = frame
            continue
        h, w = frame.shape
        x0, y0 = int(area[0] * w), int(area[1] * h)
        x1, y1 = int(np.ceil(area[2] * w)), int(np.ceil(area[3] * h))
        clean = np.zeros_like(frame)
        clean[y0:y1, x0:x1] = frame[y0:y1, x0:x1]
        result[name] = clean
    return result


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
        legacy = self.detector.legacy
        event = self.detector.feed(frame)
        if self.detector.legacy and not legacy:
            print(
                f"{datetime.now():%Y-%m-%d %H:%M:%S} Tempo real: a tradução aparece nas capturas; referência tomada depois "
                "de ela aparecer (trocas de página logo depois da tradução podem passar despercebidas)",
                file=sys.stderr, flush=True,
            )
        if event == "mudou":
            self.changed.emit()
        elif event == "parou":
            self.settled.emit()
