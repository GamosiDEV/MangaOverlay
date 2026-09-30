"""Desenho das traduções: forma do balão, quebra de linhas e hifenização (python -m pytest tests)."""

import os

import numpy as np
import pytest
from PIL import Image, ImageDraw

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QFontMetricsF, QGuiApplication  # noqa: E402

from mangaoverlay import render  # noqa: E402
from mangaoverlay.pipeline import bubble_shape  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QGuiApplication.instance() or QGuiApplication([])


def _frame(draw_shape, fill=(255, 255, 255), text=True) -> np.ndarray:
    """Retícula escura em volta (desenho) e a forma no meio, com "texto" até perto dos cantos."""
    image = Image.new("RGB", (400, 400), (90, 90, 90))
    draw = ImageDraw.Draw(image)
    for x in range(0, 400, 4):  # retícula
        draw.line((x, 0, x, 400), fill=(30, 30, 30))
    draw_shape(draw, (100, 100, 300, 300), fill)
    if text:
        for y in range(120, 290, 20):
            draw.line((118, y, 282, y), fill=(0, 0, 0), width=2)  # linhas de texto, encostando nos cantos da caixa
    return np.asarray(image)


def test_caixa_retangular_e_balao_redondo():
    box = (97, 97, 303, 303)  # a caixa do detector sai um pouco folgada
    rect = _frame(lambda d, b, f: d.rectangle(b, fill=f, outline=(0, 0, 0), width=3))
    oval = _frame(lambda d, b, f: d.ellipse(b, fill=f, outline=(0, 0, 0), width=3), text=False)
    assert bubble_shape(rect, box, (255, 255, 255)) == "rect"
    assert bubble_shape(oval, box, (255, 255, 255)) == "ellipse"


def test_hifeniza_so_palavra_que_nao_cabe_e_nunca_deixa_duas_letras_no_fim():
    font = render.base_font("")
    font.setPixelSize(20)
    metrics = QFontMetricsF(font)
    hyphenator = render._hyphenator("pt")
    width = metrics.horizontalAdvance("desconhe") + 2
    lines = render._wrap(["é", "desconhecido."], [width] * 3, metrics, hyphenator)
    # Começa na mesma linha do "é" (sem deixá-lo sozinho) e não termina em "-do."
    assert lines is not None and lines[0].startswith("é ") and lines[0].endswith("-")
    assert all(render._letters(line.split("-")[-1]) != 2 for line in lines[1:])
    # Palavras que cabem inteiras nunca são quebradas
    assert render._wrap(["uma", "casa"], [metrics.horizontalAdvance("uma casa") - 1] * 2, metrics, hyphenator) == ["uma", "casa"]


def test_elipse_da_linhas_mais_largas_no_meio():
    from PySide6.QtCore import QRectF

    widths = render._line_widths(QRectF(0, 0, 200, 100), True, 5, 30, 3)
    assert widths[1] > widths[0] and widths[1] > widths[2]
    assert render._line_widths(QRectF(0, 0, 200, 100), False, 5, 30, 3) == [200, 200, 200]


def test_fonte_de_quadrinhos_incluida():
    from PySide6.QtGui import QFontInfo

    assert QFontInfo(render.base_font(render.COMIC_FONT)).family() == render.COMIC_FONT


def test_sinais_soltos_ficam_com_a_palavra_vizinha():
    assert render._words("- Não. Sério ?!") == ["- Não.", "Sério ?!"]
    assert render._words("... e daí") == ["... e", "daí"]
    font = render.base_font("")
    font.setPixelSize(20)
    metrics = QFontMetricsF(font)
    # Já tem hífen: não quebra de novo ("Fo-da-te!")
    assert render._hyphenate("Foda-te!", "", metrics.horizontalAdvance("Fod-") + 1, metrics, render._hyphenator("pt")) is None


def test_papel_da_caixa_retangular_ignora_texto_denso():
    from mangaoverlay.pipeline import box_paper

    frame = _frame(lambda d, b, f: d.rectangle(b, fill=f, outline=(0, 0, 0), width=3), fill=(250, 245, 235))
    frame = frame.copy()
    frame[140:260, 140:260] = 60  # onomatopeia grossa no meio da caixa
    assert box_paper(frame, (97, 97, 303, 303)) == (250, 245, 235)


def test_tamanho_nunca_quebra_uma_palavra_a_forca():
    from PySide6.QtCore import QRectF

    font = render.base_font(render.COMIC_FONT)
    text = " ".join(render._words("- Não."))
    fitted = render._fit_font(font, text, QRectF(0, 0, 60, 200))
    assert QFontMetricsF(fitted).horizontalAdvance(text) <= 60.5
