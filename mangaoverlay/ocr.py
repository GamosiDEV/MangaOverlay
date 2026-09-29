"""OCR local de cada texto detectado.

- Japonês: manga-ocr (kha-white/manga-ocr-base, Apache-2.0), que lê texto vertical e estilizado de mangá.
- Coreano, chinês e inglês: EasyOCR.
"""

import re

import numpy as np
from PIL import Image

from .config import CACHE_DIR

MANGA_OCR_ID = "kha-white/manga-ocr-base"
_EASYOCR_LANGS = {"ko": ["ko", "en"], "zh-CN": ["ch_sim", "en"], "zh-TW": ["ch_tra", "en"], "en": ["en"]}


class _MangaOcr:
    """Mesma inferência e pós-processamento do pacote manga-ocr, sem as dependências extras dele."""

    def __init__(self, device: str):
        import torch
        from transformers import BertTokenizer, ViTImageProcessor, VisionEncoderDecoderModel

        self._torch = torch
        self.device = device
        self.processor = ViTImageProcessor.from_pretrained(MANGA_OCR_ID)
        # Só decodificamos: o vocabulário basta (o BertJapaneseTokenizer original exigiria o MeCab)
        self.tokenizer = BertTokenizer.from_pretrained(MANGA_OCR_ID)
        self.model = VisionEncoderDecoderModel.from_pretrained(MANGA_OCR_ID).to(device).eval()

    def read_batch(self, images: list[Image.Image]) -> list[tuple[str, float]]:
        """(texto, confiança média dos caracteres) de cada imagem."""
        if not images:
            return []
        prepared = [img.convert("L").convert("RGB") for img in images]
        pixels = self.processor(prepared, return_tensors="pt").pixel_values.to(self.device)
        with self._torch.inference_mode():
            out = self.model.generate(pixels, max_length=300, output_scores=True, return_dict_in_generate=True)
        texts = self.tokenizer.batch_decode(out.sequences, skip_special_tokens=True)
        if getattr(out, "sequences_scores", None) is not None:
            # Beam search (padrão do modelo): log-probabilidade média por caractere de cada leitura
            confidences = out.sequences_scores.exp().tolist()
        else:
            log_probs = self.model.compute_transition_scores(out.sequences, out.scores, normalize_logits=True)
            valid = out.sequences[:, 1:] != self.tokenizer.pad_token_id
            confidences = [float(row[mask].exp().mean()) if bool(mask.any()) else 0.0 for row, mask in zip(log_probs, valid)]
        results = [(self._post_process(text), float(conf)) for text, conf in zip(texts, confidences)]
        return results

    @staticmethod
    def _post_process(text: str) -> str:
        text = "".join(text.split())
        text = text.replace("…", "...")
        return re.sub("[・.]{2,}", lambda m: (m.end() - m.start()) * ".", text)


class OcrEngine:
    def __init__(self, device: str):
        self.device = device
        self._manga_ocr: _MangaOcr | None = None
        self._easyocr: dict[str, object] = {}

    def load(self, source: str):
        """Carrega (uma vez) o OCR do idioma: manga-ocr para japonês, EasyOCR para os outros."""
        if source not in _EASYOCR_LANGS:
            if self._manga_ocr is None:
                self._manga_ocr = _MangaOcr(self.device)
            return self._manga_ocr
        reader = self._easyocr.get(source)
        if reader is None:
            import easyocr

            storage = CACHE_DIR / "easyocr"
            storage.mkdir(parents=True, exist_ok=True)
            reader = easyocr.Reader(
                _EASYOCR_LANGS[source],
                gpu=self.device.startswith("cuda"),
                model_storage_directory=str(storage),
                verbose=False,
            )
            self._easyocr[source] = reader
        return reader

    def find_text_blocks(self, image: Image.Image, source: str) -> list[tuple[int, int, int, int]]:
        """Blocos de texto horizontal na página inteira (detector de texto do EasyOCR), com as linhas próximas de
        uma mesma legenda juntadas num bloco só. Usado na importação para pegar o que o detector de balões perdeu:
        legendas sobre o desenho, placas, onomatopeias em letras."""
        reader = self.load(source if source in _EASYOCR_LANGS else "en")
        horizontal, _free = reader.detect(np.asarray(image.convert("RGB")), min_size=12, text_threshold=0.6, low_text=0.35)
        lines = [(int(x0), int(y0), int(x1), int(y1)) for x0, x1, y0, y1 in (horizontal[0] if horizontal else [])]
        return _merge_lines(lines)

    def is_latin(self, crop: Image.Image) -> bool:
        """O recorte tem texto latino legível? O manga-ocr "inventa" japonês a partir de qualquer texto,
        então botões e legendas em português virariam falas; o EasyOCR em inglês desmascara esses casos."""
        text, confidence = self._read_easyocr(crop, "en")
        return confidence >= 0.5 and len(re.findall(r"[A-Za-zÀ-ÿ]", text)) >= 4

    def read(self, crops: list[Image.Image], source: str) -> list[tuple[str, float]]:
        """(texto, confiança de 0 a 1) de cada recorte."""
        if source in _EASYOCR_LANGS:
            return [self._read_easyocr(crop, source) for crop in crops]
        return self.load(source).read_batch(crops)

    def _read_easyocr(self, crop: Image.Image, source: str) -> tuple[str, float]:
        reader = self.load(source)

        # Textos pequenos são lidos melhor ampliados
        if crop.height < 64:
            factor = 64 / crop.height
            crop = crop.resize((round(crop.width * factor), 64), Image.Resampling.LANCZOS)
        lines = reader.readtext(np.asarray(crop.convert("RGB")), detail=1, paragraph=False)
        # Ordem de leitura: de cima para baixo, da esquerda para a direita
        lines = [(box, text, conf) for box, text, conf in lines if conf >= 0.2 and text.strip()]
        lines.sort(key=lambda item: (round(min(p[1] for p in item[0]) / 12), min(p[0] for p in item[0])))
        if not lines:
            return "", 0.0
        separator = "" if source.startswith("zh") else " "
        confidence = sum(conf for _box, _text, conf in lines) / len(lines)
        return separator.join(text.strip() for _box, text, _conf in lines), float(confidence)


def _merge_lines(lines: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Junta linhas que se sobrepõem na horizontal e estão a menos de ~0,8 altura de linha uma da outra."""
    blocks = [list(line) for line in lines]
    merged = True
    while merged:
        merged = False
        for i in range(len(blocks)):
            for j in range(i + 1, len(blocks)):
                a, b = blocks[i], blocks[j]
                gap = 0.8 * min(a[3] - a[1], b[3] - b[1])
                horizontal = min(a[2], b[2]) - max(a[0], b[0]) > -gap
                vertical = min(a[3], b[3]) - max(a[1], b[1]) > -gap
                if horizontal and vertical:
                    blocks[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del blocks[j]
                    merged = True
                    break
            if merged:
                break
    return [tuple(b) for b in blocks]
