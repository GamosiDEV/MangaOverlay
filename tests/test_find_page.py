"""find_page: a página de mangá é separada do que está em volta dela na tela (python -m pytest tests)."""

import numpy as np
from PIL import Image, ImageDraw

from mangaoverlay.colorize import find_page

PAGE = (200, 150, 700, 850)  # onde a página é desenhada nas telas de teste


def _screen(background: int, paper: int = 255, toolbar: int | None = None, palette: bool = False) -> Image.Image:
    """Tela 1000x1000 com uma página de mangá (quadros, calhas, balões com texto) sobre um fundo uniforme."""
    image = Image.new("RGB", (1000, 1000), (background,) * 3)
    draw = ImageDraw.Draw(image)
    if toolbar is not None:
        draw.rectangle((0, 0, 999, 120), fill=(toolbar,) * 3)
        draw.text((30, 60), "Arquivo  Editar  Ver", fill=(0, 0, 0))
        if palette:
            for i, color in enumerate([(230, 30, 30), (30, 160, 60), (40, 90, 220), (250, 200, 0)]):
                draw.ellipse((600 + 40 * i, 40, 630 + 40 * i, 70), fill=color)
    draw.rectangle(PAGE, fill=(paper,) * 3)
    # Dois quadros com uma calha branca (cor do papel) entre eles
    draw.rectangle((220, 170, 680, 480), outline=(0, 0, 0), width=4)
    draw.rectangle((220, 520, 680, 830), outline=(0, 0, 0), width=4)
    rng = np.random.default_rng(1)
    for _ in range(60):  # traços do desenho
        x, y = int(rng.integers(230, 670)), int(rng.integers(180, 820))
        draw.line((x, y, x + int(rng.integers(-30, 30)), y + int(rng.integers(-30, 30))), fill=(40, 40, 40), width=2)
    bubbles = [(260, 200, 420, 420), (480, 560, 650, 790)]
    for box in bubbles:
        draw.ellipse(box, fill=(paper,) * 3, outline=(0, 0, 0), width=3)
        cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
        draw.rectangle((cx - 8, cy - 50, cx + 8, cy + 50), fill=(20, 20, 20))  # "texto"
    return image


BUBBLES = [(260, 200, 420, 420), (480, 560, 650, 790)]


def _near(box, expected, tolerance: int = 6) -> bool:
    return box is not None and all(abs(a - b) <= tolerance for a, b in zip(box, expected))


def test_fundo_escuro_do_leitor():
    assert _near(find_page(_screen(background=30), BUBBLES), PAGE)


def test_fundo_cinza_claro_como_no_paint():
    # Antes, o cinza claro (243) não contava como borda e a cor vazava para a interface do programa
    assert _near(find_page(_screen(background=243), BUBBLES), PAGE)


def test_barra_de_ferramentas_quase_branca_acima():
    screen = _screen(background=243, toolbar=249, palette=True)
    box = find_page(screen, BUBBLES)
    assert _near(box, PAGE)


def test_scan_amarelado_em_site_branco():
    assert _near(find_page(_screen(background=255, paper=238), BUBBLES), PAGE)


def test_calha_branca_entre_quadros_nao_interrompe():
    # Os balões estão em quadros diferentes; a página inteira precisa vir, não só um quadro
    box = find_page(_screen(background=243), [BUBBLES[0]])
    assert _near(box, PAGE)


def test_sem_baloes():
    assert find_page(_screen(background=30), []) is None


def test_pagina_encostada_na_barra_de_ferramentas():
    """Zoom de 100% no Paint: a página encosta na barra, separada só pela linha fina da borda dela (tom 235)."""
    screen = _screen(background=243)
    draw = ImageDraw.Draw(screen)
    draw.rectangle((0, 0, 999, PAGE[1] - 2), fill=(249, 249, 249))  # barra de ferramentas até a página
    draw.line((0, PAGE[1] - 1, 999, PAGE[1] - 1), fill=(235, 235, 235))  # borda da barra
    for x in range(40, 900, 90):
        draw.text((x, PAGE[1] - 20), "Tools", fill=(40, 40, 40))  # rótulos logo acima da borda
    for i, color in enumerate([(230, 30, 30), (30, 160, 60), (40, 90, 220)]):
        draw.ellipse((600 + 40 * i, 20, 630 + 40 * i, 50), fill=color)
    box = find_page(screen, BUBBLES)
    assert box is not None and abs(box[1] - PAGE[1]) <= 6, box
