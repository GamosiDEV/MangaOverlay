"""Detecção de balões e textos com o RT-DETR-v2 treinado em mangá/webtoon/manhua/quadrinhos.

Modelo: ogkalu/comic-text-and-bubble-detector (Apache-2.0), classes bubble, text_bubble e text_free.
"""

from dataclasses import dataclass

import numpy as np
from PIL import Image

MODEL_ID = "ogkalu/comic-text-and-bubble-detector"

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 (pixels da imagem)

# Balões de mangá passam de 0,9; elementos de interface (botões, cartões) confundidos com balões ficam abaixo de 0,8.
_THRESHOLDS = {"bubble": 0.8, "text_bubble": 0.35, "text_free": 0.6}
# Arquivos importados (a imagem inteira é a página: não há menus nem sites para confundir): limiares baixos,
# textos soltos e texto sem balão confiável em volta. Medido num volume real: ~18% mais falas capturadas.
_FILE_THRESHOLDS = {"bubble": 0.3, "text_bubble": 0.2, "text_free": 0.2}
# Recuo do retângulo inscrito numa elipse (1 - 1/√2) / 2 ≈ 0,146
_BUBBLE_INSET = 0.16


@dataclass
class Region:
    text_box: Box  # onde está o texto original (é coberto)
    area: Box  # onde a tradução pode ser escrita
    bubble: Box | None  # contorno do balão, quando o texto está dentro de um
    score: float = 1.0  # confiança do detector (perfil de arquivo: decide a segunda leitura em japonês)


@dataclass
class Detection:
    regions: list[Region]  # textos a traduzir
    bubbles: list[Box]  # balões confiáveis (também servem para localizar a página na tela)


def _inset(box: Box, fraction: float) -> Box:
    x0, y0, x1, y1 = box
    dx, dy = (x1 - x0) * fraction, (y1 - y0) * fraction
    return round(x0 + dx), round(y0 + dy), round(x1 - dx), round(y1 - dy)


def _union(a: Box, b: Box) -> Box:
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _contains_center(outer: Box, inner: Box) -> bool:
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def _area(box: Box) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _tiles(width: int, height: int) -> list[tuple[int, int, int]]:
    """Quadrados sobrepostos cobrindo a imagem (x, y, lado).

    O modelo redimensiona tudo para 640×640; um print 16:9 inteiro ficaria achatado e os textos pequenos sumiriam.
    """
    side = min(width, height)
    long_side = max(width, height)
    count = max(1, int(np.ceil(long_side / side - 0.15)))
    if count == 1:
        return [(0, 0, side)] if width == height else [(0, 0, long_side)]
    step = (long_side - side) / (count - 1)
    return [(round(i * step), 0, side) if width > height else (0, round(i * step), side) for i in range(count)]


class Detector:
    def __init__(self, device: str):
        import torch
        from transformers import RTDetrImageProcessor, RTDetrV2ForObjectDetection

        self._torch = torch
        self.device = device
        self.processor = RTDetrImageProcessor.from_pretrained(MODEL_ID)
        self.model = RTDetrV2ForObjectDetection.from_pretrained(MODEL_ID).to(device).eval()
        self.labels = self.model.config.id2label

    def _raw_detect(self, image: Image.Image, thresholds: dict[str, float] | None = None) -> list[tuple[str, float, Box]]:
        thresholds = thresholds or _THRESHOLDS
        torch = self._torch
        width, height = image.size
        tiles = _tiles(width, height)
        crops = []
        for x, y, side in tiles:
            if len(tiles) == 1 and side == max(width, height):
                crops.append(image)  # imagem inteira (quadrada ou uma só peça)
            else:
                crops.append(image.crop((x, y, x + side, y + side)))

        results = []
        for start in range(0, len(crops), 8):  # webtoons muito altos viram dezenas de pedaços: lotes de até 8
            chunk = crops[start : start + 8]
            inputs = self.processor(images=chunk, return_tensors="pt").to(self.device)
            with torch.inference_mode():
                outputs = self.model(**inputs)
            sizes = [(c.height, c.width) for c in chunk]
            results += self.processor.post_process_object_detection(outputs, target_sizes=sizes, threshold=0.25)

        detections = []
        for (tx, ty, _side), crop, result in zip(tiles, crops, results):
            for score, label, box in zip(result["scores"], result["labels"], result["boxes"]):
                name = self.labels[int(label)]
                score = float(score)
                if score < thresholds.get(name, 0.5):
                    continue
                x0, y0, x1, y1 = (float(v) for v in box)
                # Caixas cortadas pela borda interna de um pedaço aparecem inteiras no vizinho
                margin = 3
                if len(tiles) > 1 and (
                    (x0 < margin and tx > 0)
                    or (y0 < margin and ty > 0)
                    or (x1 > crop.width - margin and tx + crop.width < width)
                    or (y1 > crop.height - margin and ty + crop.height < height)
                ):
                    continue
                detections.append((name, score, (round(x0 + tx), round(y0 + ty), round(x1 + tx), round(y1 + ty))))
        return self._nms(detections)

    def _nms(self, detections):
        from torchvision.ops import batched_nms

        torch = self._torch
        if not detections:
            return []
        names = sorted({d[0] for d in detections})
        boxes = torch.tensor([d[2] for d in detections], dtype=torch.float32)
        scores = torch.tensor([d[1] for d in detections])
        classes = torch.tensor([names.index(d[0]) for d in detections])
        keep = batched_nms(boxes, scores, classes, iou_threshold=0.4)
        return [detections[i] for i in keep.tolist()]

    def detect(self, image: Image.Image, include_free_text: bool = True, profile: str = "screen") -> Detection:
        """`profile`: "screen" (captura da tela, com filtros contra textos de menus e sites) ou "file"
        (página de arquivo importado: aceita tudo o que o modelo enxergar como texto)."""
        if profile == "file":
            return self._detect_file(image)
        detections = self._raw_detect(image)
        bubbles = [box for name, _, box in detections if name == "bubble"]
        texts = [box for name, _, box in detections if name == "text_bubble"]
        free = [box for name, _, box in detections if name == "text_free"]

        # Junta os textos de um mesmo balão (ex.: colunas verticais detectadas separadas)
        grouped: dict[int, Box] = {}
        for text in texts:
            owners = [i for i, b in enumerate(bubbles) if _contains_center(b, text)]
            if not owners:
                continue
            owner = min(owners, key=lambda i: _area(bubbles[i]))  # o balão mais justo
            grouped[owner] = _union(grouped[owner], text) if owner in grouped else text

        # Textos "de balão" sem balão confiável em volta são descartados: costumam ser da interface do sistema.
        regions = [
            Region(text_box=text, area=_union(_inset(bubbles[i], _BUBBLE_INSET), text), bubble=bubbles[i])
            for i, text in grouped.items()
        ]
        if include_free_text:
            # Texto solto (narração, onomatopeias, HQs sem balões) e texto "de balão" sem balão em volta. O mesmo
            # texto costuma vir marcado das duas formas: fica uma vez só.
            loose: list[Box] = []
            for t in free + [t for t in texts if not any(_contains_center(b, t) for b in bubbles)]:
                if not any(_contains_center(b, t) for b in bubbles) and not any(_contains_center(k, t) for k in loose):
                    loose.append(t)
            if bubbles:
                # O detector também marca textos de menus e sites como "texto livre". Com balões na tela, só vale o
                # que estiver na faixa da página onde eles estão (leitores de mangá centralizam a página).
                left = min(b[0] for b in bubbles)
                right = max(b[2] for b in bubbles)
                margin = (right - left) * 0.25
                loose = [t for t in loose if left - margin <= (t[0] + t[2]) / 2 <= right + margin]
            # Sem balões (HQ com o texto sobre o desenho), vale tudo: o OCR descarta o que não estiver na escrita do
            # idioma de origem, como menus em português ou inglês
            regions += [Region(text_box=t, area=t, bubble=None) for t in loose]

        return Detection(self._clip(regions, image.size), bubbles)

    def _detect_file(self, image: Image.Image) -> Detection:
        # Limiares baixos: o OCR depois descarta o que não for texto legível
        detections = self._raw_detect(image, _FILE_THRESHOLDS)
        bubbles = [box for name, _, box in detections if name == "bubble"]
        grouped: dict[int, tuple[Box, float]] = {}
        regions = []
        for name, score, box in detections:
            if name == "text_bubble":
                owners = [i for i, b in enumerate(bubbles) if _contains_center(b, box)]
                if owners:
                    owner = min(owners, key=lambda i: _area(bubbles[i]))
                    if owner in grouped:
                        previous, previous_score = grouped[owner]
                        grouped[owner] = (_union(previous, box), max(previous_score, score))
                    else:
                        grouped[owner] = (box, score)
                else:
                    # Balão de formato incomum (grito, pensamento) que o modelo não reconheceu: fica mesmo assim
                    regions.append(Region(text_box=box, area=_inset(box, -0.1), bubble=None, score=score))
            elif name == "text_free" and not any(_contains_center(b, box) for b in bubbles):
                # Narração, onomatopeias, placas e textos do cenário fora dos balões
                regions.append(Region(text_box=box, area=box, bubble=None, score=score))
        regions = [
            Region(text_box=text, area=_union(_inset(bubbles[i], _BUBBLE_INSET), text), bubble=bubbles[i], score=score)
            for i, (text, score) in grouped.items()
        ] + regions
        return Detection(self._clip(regions, image.size), bubbles)

    @staticmethod
    def _clip(regions: list[Region], size: tuple[int, int]) -> list[Region]:
        width, height = size
        clipped = []
        for r in regions:
            clip = lambda b: (max(0, b[0]), max(0, b[1]), min(width, b[2]), min(height, b[3]))  # noqa: E731
            text_box, area = clip(r.text_box), clip(r.area)
            if _area(text_box) >= 64:
                clipped.append(Region(text_box, area, r.bubble, r.score))
        return clipped
