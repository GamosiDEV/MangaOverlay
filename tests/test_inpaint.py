"""Reconstrução do desenho (inpainting), com um "modelo" falso: só a mecânica de janela, máscara e colagem."""

import numpy as np
from PIL import Image

from mangaoverlay.inpaint import MODEL_SIZE, Inpainter, mask_box, window


def _gray_model(image, mask):
    """Devolve cinza médio (128) onde a máscara manda reconstruir, e a imagem de entrada no resto."""
    out = (image * 255).astype(np.uint8)
    out[mask] = 128
    return out


def test_janela_tem_o_texto_e_o_desenho_em_volta():
    box = (100, 100, 140, 120)
    x0, y0, x1, y1 = window(box, (1000, 1000))
    assert x0 < 100 and y0 < 100 and x1 > 140 and y1 > 120
    assert x1 - x0 == y1 - y0  # quadrada longe da borda
    # Na borda da página, recortada
    assert window((0, 0, 30, 30), (200, 200))[:2] == (0, 0)


def test_so_a_area_do_texto_muda():
    page = np.zeros((400, 300, 3), dtype=np.uint8)
    page[:, :, :] = 30
    image = Image.fromarray(page)
    box = (100, 150, 180, 190)
    out = np.asarray(Inpainter("cpu", model=_gray_model).erase(image, [box])).astype(int)
    mx0, my0, mx1, my1 = mask_box(box, image.size)
    inside = out[my0 + 2 : my1 - 2, mx0 + 2 : mx1 - 2]
    assert abs(inside.mean() - 128) < 3  # reconstruído
    outside = out.copy()
    outside[my0:my1, mx0:mx1] = 30
    assert (outside == 30).all()  # o resto da página intacto


def test_modelo_recebe_512():
    seen = []

    def model(image, mask):
        seen.append((image.shape, mask.shape, float(image.max())))
        return (image * 255).astype(np.uint8)

    Inpainter("cpu", model=model).erase(Image.new("RGB", (900, 1300), (255, 255, 255)), [(10, 10, 700, 60)])
    assert seen == [((MODEL_SIZE, MODEL_SIZE, 3), (MODEL_SIZE, MODEL_SIZE), 1.0)]
