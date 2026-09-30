"""Reconstrução do desenho por baixo de textos fora dos balões (inpainting), para os capítulos gerados.

Modelo: LaMa (Suvorov et al., Apache-2.0), exportado em ONNX por Carve/LaMa-ONNX (Apache-2.0) e convertido para
PyTorch como o colorizador. Entrada fixa de 512×512: cada texto é apagado numa janela com o desenho em volta (o
contexto que o modelo usa para reconstruir), reduzida ou ampliada para 512, e só a área do texto volta para a página.
"""

from collections.abc import Callable

import numpy as np
from PIL import Image

MODEL_REPO = "Carve/LaMa-ONNX"
MODEL_FILE = "lama_fp32.onnx"
MODEL_SIZE = 512
# Folga da máscara em volta da caixa do texto: a caixa do detector é justa e deixaria a borda das letras
_MASK_PAD = 4
# Desenho em volta do texto que entra na janela (fração do maior lado do texto; pelo menos 48 px)
_CONTEXT = 0.75
_MIN_CONTEXT = 48

Box = tuple[int, int, int, int]


def mask_box(box: Box, size: tuple[int, int]) -> Box:
    x0, y0, x1, y1 = box
    return max(0, x0 - _MASK_PAD), max(0, y0 - _MASK_PAD), min(size[0], x1 + _MASK_PAD), min(size[1], y1 + _MASK_PAD)


def window(box: Box, size: tuple[int, int]) -> Box:
    """Janela quadrada centrada no texto, com o desenho em volta; recortada na borda da página."""
    x0, y0, x1, y1 = box
    side = max(x1 - x0, y1 - y0) + 2 * max(_MIN_CONTEXT, round(max(x1 - x0, y1 - y0) * _CONTEXT))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    wx0, wy0 = max(0, round(cx - side / 2)), max(0, round(cy - side / 2))
    return wx0, wy0, min(size[0], wx0 + side), min(size[1], wy0 + side)


def _lama(device: str) -> Callable[[np.ndarray, np.ndarray], np.ndarray]:
    """O LaMa em PyTorch: (imagem 512×512×3 em 0–1, máscara 512×512 booleana) -> imagem 512×512×3 em uint8."""
    import onnx
    import torch
    from huggingface_hub import hf_hub_download
    from onnx2torch import convert

    model = convert(onnx.load(hf_hub_download(MODEL_REPO, MODEL_FILE))).to(device).eval()

    def run(image: np.ndarray, mask: np.ndarray) -> np.ndarray:
        image_t = torch.from_numpy(image).permute(2, 0, 1)[None].float().to(device)
        mask_t = torch.from_numpy(mask.astype(np.float32))[None, None].to(device)
        with torch.no_grad():
            out = model(image_t, mask_t)  # sai em 0–255
        return out[0].permute(1, 2, 0).clamp(0, 255).byte().cpu().numpy()

    return run


class Inpainter:
    """`model`: o LaMa (padrão) ou, nos testes, qualquer função com a mesma assinatura (ver _lama)."""

    def __init__(self, device: str, model: Callable[[np.ndarray, np.ndarray], np.ndarray] | None = None):
        self._model = model or _lama(device)

    def erase(self, image: Image.Image, boxes: list[Box]) -> Image.Image:
        """A página com o desenho reconstruído no lugar de cada caixa de texto."""
        pixels = np.asarray(image.convert("RGB")).copy()
        for box in boxes:
            mx0, my0, mx1, my1 = mask_box(box, image.size)
            wx0, wy0, wx1, wy1 = window((mx0, my0, mx1, my1), image.size)
            crop = pixels[wy0:wy1, wx0:wx1]  # vista: o que mudar aqui muda a página
            mask = np.zeros(crop.shape[:2], dtype=np.uint8)
            mask[my0 - wy0 : my1 - wy0, mx0 - wx0 : mx1 - wx0] = 1
            if not mask.any():
                continue
            small = np.asarray(Image.fromarray(crop).resize((MODEL_SIZE, MODEL_SIZE), Image.Resampling.BICUBIC), dtype=np.float32)
            small_mask = np.asarray(Image.fromarray(mask * 255).resize((MODEL_SIZE, MODEL_SIZE), Image.Resampling.NEAREST)) > 0
            out = self._model(small / 255.0, small_mask)
            restored = np.asarray(Image.fromarray(out).resize((crop.shape[1], crop.shape[0]), Image.Resampling.BICUBIC))
            crop[mask == 1] = restored[mask == 1]
        return Image.fromarray(pixels)
