"""Desenho das traduções: cobre o texto original e encaixa a tradução no balão, no maior tamanho que couber.

Dentro de um balão, o texto segue a forma dele: num balão redondo cada linha tem a largura da elipse naquela altura
(as do meio são mais largas); numa caixa retangular de narração, a caixa inteira é usada e coberta. Palavras maiores
que a linha são hifenizadas (pyphen), mas só quando isso deixa a fonte bem maior.
"""

import math
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QPainter, QPainterPath, QPen

from .pipeline import OverlayItem

_FLAGS = Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap
_MIN_SIZE = 7
# Fonte de quadrinhos incluída no app (SIL Open Font License, ver assets/fonts/OFL.txt)
COMIC_FONT = "Comic Neue"
_FONT_FILES = [Path(__file__).resolve().parent.parent / "assets" / "fonts" / "ComicNeue-Bold.ttf"]
# Só hifeniza se a fonte ficar pelo menos 15% maior que sem hifenizar
_HYPHEN_GAIN = 1.15
_HYPHEN_LANGUAGES = {"pt": "pt_BR", "en": "en_US", "es": "es", "fr": "fr", "de": "de_DE", "it": "it_IT"}
# Balão muito maior que o texto: provavelmente um quadro confundido com balão; escreve só em volta do texto
_MAX_BUBBLE_TO_TEXT = 8
_fonts_registered = False


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


def register_fonts() -> None:
    """Deixa a fonte incluída disponível (nas Configurações e no desenho). Precisa de uma aplicação Qt."""
    global _fonts_registered
    if _fonts_registered:
        return
    _fonts_registered = True
    for path in _FONT_FILES:
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))


def base_font(family: str) -> QFont:
    register_fonts()
    font = QFont(family) if family else QFont()
    if not family:
        font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setBold(True)
    return font


# --- texto que acompanha a forma do balão ------------------------------------------------


@lru_cache(maxsize=8)
def _hyphenator(language: str):
    code = _HYPHEN_LANGUAGES.get(language.split("-")[0])
    if code is None:
        return None
    try:
        import pyphen

        return pyphen.Pyphen(lang=code)
    except (ImportError, KeyError):  # instalação antiga sem o pyphen: só não hifeniza
        return None


def _line_widths(region: QRectF, ellipse: bool, top: float, line_height: float, count: int) -> list[float]:
    """Largura disponível para cada linha. Na elipse, vale a borda da linha mais longe do centro."""
    if not ellipse:
        return [region.width()] * count
    cy, a, b = region.center().y(), region.width() / 2, region.height() / 2
    widths = []
    for i in range(count):
        far = max(abs(top + i * line_height - cy), abs(top + (i + 1) * line_height - cy))
        widths.append(0.0 if far >= b else 2 * a * math.sqrt(1 - (far / b) ** 2))
    return widths


def _letters(text: str) -> int:
    return sum(ch.isalpha() for ch in text)


def _words(text: str) -> list[str]:
    """Palavras para a quebra de linha. Sinais soltos ficam grudados na palavra vizinha (espaço não separável), para
    um "-" ou um "!" nunca ficar sozinho numa linha."""
    words: list[str] = []
    glue = ""
    for token in text.split():
        if not any(ch.isalnum() for ch in token):
            if words and not glue and token[0] not in "-—–«“(\"'":
                words[-1] += "\u00a0" + token  # "!" e "?!" vão com a palavra de antes
            else:
                glue += token + "\u00a0"  # travessão e aspas de abertura vão com a de depois
            continue
        words.append(glue + token)
        glue = ""
    if glue:
        words.append(glue.rstrip("\u00a0"))
    return words


def _hyphenate(word: str, prefix: str, width: float, metrics: QFontMetricsF, hyphenator) -> tuple[str, str] | None:
    """O maior começo da palavra que cabe na linha (depois de `prefix`) com o hífen; pelo menos 2 letras antes e 3
    depois ("desconheci-do." não vale). Palavras que já têm hífen ("Foda-se") não são quebradas de novo."""
    if "-" in word:
        return None
    for head, tail in hyphenator.iterate(word):
        if _letters(head) >= 2 and _letters(tail) >= 3 and metrics.horizontalAdvance(f"{prefix}{head}-") <= width:
            return head, tail
    return None


def _wrap(words: list[str], widths: list[float], metrics: QFontMetricsF, hyphenator) -> list[str] | None:
    """Quebra gulosa em linhas de larguras diferentes. Uma palavra só é hifenizada quando não cabe inteira nem numa
    linha vazia; aí ela já começa na linha atual (sem deixar um "o" ou um "é" sozinho numa linha). None se não couber
    no número de linhas."""
    lines: list[str] = []
    current = ""
    pending = list(words)
    while pending:
        if len(lines) >= len(widths):
            return None
        width = widths[len(lines)]
        word = pending.pop(0)
        candidate = f"{current} {word}" if current else word
        if metrics.horizontalAdvance(candidate) <= width:
            current = candidate
            continue
        too_long = metrics.horizontalAdvance(word) > max(widths[len(lines) :])
        split = None
        if hyphenator is not None and too_long:
            split = _hyphenate(word, f"{current} " if current else "", width, metrics, hyphenator)
        if split is not None:
            lines.append(f"{current} {split[0]}-" if current else f"{split[0]}-")
            current = ""
            pending.insert(0, split[1])
        elif current:
            lines.append(current)
            current = ""
            pending.insert(0, word)
        else:
            return None  # palavra maior que a linha, sem como hifenizar
    if current:
        if len(lines) >= len(widths):
            return None
        lines.append(current)
    return lines


def _place(font: QFont, words: list[str], region: QRectF, ellipse: bool, hyphenator) -> list[tuple[QRectF, str]] | None:
    """Linhas centralizadas na região, do menor número de linhas que couber."""
    metrics = QFontMetricsF(font)
    line_height = metrics.height()
    count = 1
    while count * line_height <= region.height():
        top = region.center().y() - count * line_height / 2
        widths = _line_widths(region, ellipse, top, line_height, count)
        if min(widths) > 0:
            lines = _wrap(words, widths, metrics, hyphenator)
            if lines is not None:
                return [(QRectF(region.left(), top + i * line_height, region.width(), line_height), line) for i, line in enumerate(lines)]
        count += 1
    return None


def _search(base: QFont, words: list[str], region: QRectF, ellipse: bool, hyphenator) -> tuple[int, list] | None:
    low, high = _MIN_SIZE, max(_MIN_SIZE, int(min(region.height(), region.width() * 0.5)))
    font = QFont(base)
    best = None
    while low <= high:
        size = (low + high) // 2
        font.setPixelSize(size)
        placed = _place(font, words, region, ellipse, hyphenator)
        if placed is not None:
            best, low = (size, placed), size + 1
        else:
            high = size - 1
    return best


@lru_cache(maxsize=1024)
def _shaped_layout(
    text: str, region: tuple[float, float, float, float], ellipse: bool, family: str, language: str
) -> tuple[int, tuple[tuple[tuple[float, float, float, float], str], ...]] | None:
    """(tamanho da fonte, linhas) do texto na região; None se não couber nem no tamanho mínimo. Guardado: o overlay
    redesenha a cada atualização da janela."""
    base = base_font(family)
    words = _words(text)
    if not words:
        return None
    area = QRectF(*region)
    plain = _search(base, words, area, ellipse, None)
    hyphenator = _hyphenator(language) if language else None
    if hyphenator is not None:
        hyphenated = _search(base, words, area, ellipse, hyphenator)
        if hyphenated is not None and (plain is None or hyphenated[0] >= plain[0] * _HYPHEN_GAIN):
            plain = hyphenated
    if plain is None:
        return None
    size, lines = plain
    return size, tuple(((r.x(), r.y(), r.width(), r.height()), line) for r, line in lines)


# --- desenho --------------------------------------------------------------------------------


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
        region, ellipse = None, True
        if item.bubble is not None:
            bubble = _rect(item.bubble)
            side = min(bubble.width(), bubble.height())
            plausible = bubble.width() * bubble.height() <= _MAX_BUBBLE_TO_TEXT * max(1.0, fill.width() * fill.height())
            if item.shape == "rect":
                # Caixa de narração: por dentro do contorno só há papel e texto, então cobre a caixa inteira
                inner = bubble.adjusted(*(v * max(3.0, side * 0.05) for v in (1, 1, -1, -1)))
                box = QPainterPath()
                box.addRect(inner)
                cover = box if plausible else cover.intersected(box)
                region, ellipse = inner.adjusted(*(v * max(2.0, side * 0.04) for v in (1, 1, -1, -1))), False
            else:
                # A caixa do texto vem folgada e às vezes encosta no contorno: limita ao interior do balão
                inner = QPainterPath()
                inner.addEllipse(bubble.adjusted(*(v * side * 0.04 for v in (1, 1, -1, -1))))
                cover = cover.intersected(inner)
                region = bubble.adjusted(*(v * side * 0.09 for v in (1, 1, -1, -1)))
            if not plausible:
                region = None
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawPath(cover)

        layout = None
        if region is not None and region.width() > 4 and region.height() > 4:
            key = (region.x(), region.y(), region.width(), region.height())
            layout = _shaped_layout(item.text, key, ellipse, font.family(), item.language)
        area = _rect(item.area).adjusted(2, 2, -2, -2)
        text = " ".join(_words(item.text))  # sinais soltos grudados também no desenho sem a forma do balão
        fitted = _fit_font(font, text, area)
        # A forma do balão só é usada se a letra ficar pelo menos do tamanho do jeito antigo (retângulo do texto): em
        # balões cheios de texto, as linhas curtas do alto e de baixo da elipse às vezes rendem menos
        if layout is not None and layout[0] >= fitted.pixelSize():
            size, lines = layout
            fitted.setPixelSize(size)
            painter.setFont(fitted)
            painter.setPen(foreground)
            for rect, line in lines:
                painter.drawText(QRectF(*rect), Qt.AlignmentFlag.AlignCenter, line)
            continue

        painter.setFont(fitted)
        if item.bubble is None:
            # Fora dos balões o texto pode passar sobre o desenho: halo na cor do fundo para ler melhor
            painter.setPen(background)
            halo = max(1.0, fitted.pixelSize() / 10)
            for dx, dy in ((-1, -1), (-1, 1), (1, -1), (1, 1), (0, -1), (0, 1), (-1, 0), (1, 0)):
                painter.drawText(area.translated(dx * halo, dy * halo), _FLAGS, text)
        painter.setPen(foreground)
        painter.drawText(area, _FLAGS, text)
