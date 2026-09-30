"""Versão colorida da página, sem alterar o conteúdo.

O modelo (manga-colorization-v2, exportado em ONNX e convertido para PyTorch) roda numa resolução
reduzida; da saída dele só aproveitamos a cor. A luminância vem da captura original em resolução
total, então traço, retículas e texto continuam exatamente como estavam.

Origem do modelo: github.com/qweasdd/manga-colorization-v2. O repositório original não declara
licença; o espelho usado aqui (ifritraen/manga-colorization-v2-fp32) declara Apache-2.0.
Trate como uso pessoal.
"""

import numpy as np
from PIL import Image

from .detector import Box

MODEL_REPO = "ifritraen/manga-colorization-v2-fp32"
MODEL_FILE = "manga_colorization_v2_fp32.onnx"
# Largura (páginas em pé) ou altura/1,5 (páginas deitadas) em que o modelo roda, como no original
MODEL_SIZE = 576


class Colorizer:
    def __init__(self, device: str):
        import onnx
        import torch
        from huggingface_hub import hf_hub_download
        from onnx2torch import convert

        self._torch = torch
        self.device = device
        path = hf_hub_download(MODEL_REPO, MODEL_FILE)
        self.model = convert(onnx.load(path)).to(device).eval()

    def colorize(self, page: Image.Image) -> Image.Image:
        """Mesma dimensão da entrada; só a cor muda."""
        torch = self._torch
        gray = page.convert("L")
        width, height = gray.size
        if height >= width:
            size = (MODEL_SIZE, max(32, round(height * MODEL_SIZE / width)))
        else:
            size = (max(32, round(width * MODEL_SIZE * 1.5 / height)), round(MODEL_SIZE * 1.5))
        small = np.asarray(gray.resize(size, Image.Resampling.BOX), dtype=np.float32) / 255
        # Dimensões múltiplas de 32, completando com branco (como o original)
        pad_h, pad_w = -small.shape[0] % 32, -small.shape[1] % 32
        small = np.pad(small, ((0, pad_h), (0, pad_w)), constant_values=1.0)

        x = torch.zeros(1, 5, *small.shape, device=self.device)  # canais 1-4: dicas de cor (nenhuma)
        x[0, 0] = torch.from_numpy(small).to(self.device)
        with torch.inference_mode():
            rgb = self.model(x)[0].clamp(0, 1)
        rgb = rgb[:, : size[1], : size[0]].permute(1, 2, 0).mul(255).byte().cpu().numpy()

        # Cor do modelo (Cb, Cr ampliados) + luminância original em resolução total
        _, cb, cr = Image.fromarray(rgb).convert("YCbCr").split()
        cb = cb.resize((width, height), Image.Resampling.BICUBIC)
        cr = cr.resize((width, height), Image.Resampling.BICUBIC)
        return Image.merge("YCbCr", (gray, cb, cr)).convert("RGB")


# Diferença de tom (0-255) a partir da qual uma faixa uniforme já não é o papel da página
_PAPER_TOLERANCE = 3


def _boundary(line: np.ndarray, saturation: np.ndarray, paper: float) -> tuple[bool, bool]:
    """(tem cor de interface, é uma faixa uniforme que não é o papel) para uma linha/coluna da imagem.

    O papel é a cor do fundo dos balões. Uma faixa uniforme de outro tom é o fundo em volta da página: escuro no
    leitor, cinza claro no Paint e em visualizadores, branco num site com um scan amarelado. As calhas entre os
    quadros têm a cor do papel e não interrompem.
    """
    colored = float((saturation > 40).mean()) > 0.02
    mean = float(line.mean())
    uniform = float(line.std()) < 5 and (mean < 180 or abs(mean - paper) > _PAPER_TOLERANCE)
    return colored, uniform


def _ui_separator(probe: list[tuple[np.ndarray, np.ndarray]], paper: float) -> bool:
    """Borda fina de uma barra de ferramentas colada na página (zoom que enche a janela): uma linha uniforme CLARA de
    tom diferente do papel, e depois dela nada com o tom do papel. As bordas dos quadros do mangá são escuras e, depois
    delas, vem a calha (papel), então não param aqui."""
    line = probe[0][0]
    mean = float(line.mean())
    if len(probe) < 2 or float(line.std()) >= 5 or not 180 <= mean < paper - _PAPER_TOLERANCE:
        return False
    beyond = np.concatenate([p[0] for p in probe[1:]])
    return abs(float(beyond.mean()) - paper) > _PAPER_TOLERANCE


def _paper(gray: np.ndarray, bubbles: list[Box], scale: int) -> float:
    """Tom do papel: a mediana dos pixels claros dentro dos balões (o texto e o contorno são escuros)."""
    inside = [gray[b[1] // scale : b[3] // scale, b[0] // scale : b[2] // scale].ravel() for b in bubbles]
    pixels = np.concatenate(inside) if inside else np.empty(0)
    light = pixels[pixels > 180]
    return float(np.median(light)) if light.size else 255.0


def find_page(image: Image.Image, bubbles: list[Box]) -> Box | None:
    """Retângulo da página de mangá na tela, expandido a partir dos balões até a borda da página.

    Para numa faixa uniforme de 12 px ou mais que não tenha o tom do papel (fundo do leitor, do visualizador ou do
    site), ou em pixels coloridos (interface do navegador, outras janelas). Os vãos brancos entre quadros não
    interrompem.
    """
    if not bubbles:
        return None
    scale = 2  # trabalha em meia resolução, pela média de cada bloco 2x2: uma linha de 1 px não some
    full = np.asarray(image.convert("RGB"), dtype=np.int16)
    h2, w2 = full.shape[0] // scale * scale, full.shape[1] // scale * scale
    rgb = full[:h2, :w2].reshape(h2 // scale, scale, w2 // scale, scale, 3).mean(axis=(1, 3))
    gray = rgb.mean(axis=2)
    saturation = rgb.max(axis=2) - rgb.min(axis=2)
    h, w = gray.shape
    x0 = max(0, min(b[0] for b in bubbles) // scale)
    y0 = max(0, min(b[1] for b in bubbles) // scale)
    x1 = min(w, max(b[2] for b in bubbles) // scale)
    y1 = min(h, max(b[3] for b in bubbles) // scale)
    run = 12 // scale
    paper = _paper(gray, bubbles, scale)

    def advance(side: str) -> bool:
        """Tenta avançar um lado; retorna False se chegou na borda da página."""
        nonlocal x0, y0, x1, y1
        # Olha `run` linhas adiante para distinguir o fundo do leitor de um detalhe uniforme da página
        probe = []
        for step in range(run):
            if side == "left":
                x = x0 - 1 - step
                if x < 0:
                    break
                probe.append((gray[y0:y1, x], saturation[y0:y1, x]))
            elif side == "right":
                x = x1 + step
                if x >= w:
                    break
                probe.append((gray[y0:y1, x], saturation[y0:y1, x]))
            elif side == "top":
                y = y0 - 1 - step
                if y < 0:
                    break
                probe.append((gray[y, x0:x1], saturation[y, x0:x1]))
            else:
                y = y1 + step
                if y >= h:
                    break
                probe.append((gray[y, x0:x1], saturation[y, x0:x1]))
        if not probe:
            return False
        colored, uniform = _boundary(*probe[0], paper)
        if colored:
            return False
        if _ui_separator(probe, paper):
            return False
        if uniform and len(probe) == run and all(_boundary(*p, paper)[1] for p in probe):
            return False
        if side == "left":
            x0 -= 1
        elif side == "right":
            x1 += 1
        elif side == "top":
            y0 -= 1
        else:
            y1 += 1
        return True

    active = {"left", "right", "top", "bottom"}
    while active:
        for side in list(active):
            if not advance(side):
                active.discard(side)
    return x0 * scale, y0 * scale, min(image.width, x1 * scale), min(image.height, y1 * scale)
