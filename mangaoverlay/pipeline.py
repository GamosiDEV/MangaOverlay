"""Do print da tela até os itens desenhados na sobreposição. Roda fora da thread da interface."""

import threading
import time
from collections import deque
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from PIL import Image

from . import credentials, llm_anthropic, llm_openai, translators
from .colorize import Colorizer, find_page
from .config import VISION_ENGINES, Config
from .db import Database, TranslationKey, normalize
from .detector import Box, Detector, Region
from .languages import AUTO, looks_like
from .ocr import OcrEngine
from .translators import Line, Result


class PipelineError(Exception):
    pass


@dataclass
class OverlayItem:
    fill: Box  # retângulo que cobre o texto original
    area: Box  # onde a tradução é escrita
    text: str
    original: str
    background: tuple[int, int, int]
    bubble: Box | None  # a cobertura não passa do interior do balão (não apaga o contorno)


def _dhash(image: Image.Image) -> int:
    small = np.asarray(image.convert("L").resize((17, 16), Image.Resampling.BILINEAR), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def _expand(box: Box, size: tuple[int, int]) -> Box:
    x0, y0, x1, y1 = box
    pad = max(3, round(min(x1 - x0, y1 - y0) * 0.06))
    return max(0, x0 - pad), max(0, y0 - pad), min(size[0], x1 + pad), min(size[1], y1 + pad)


def _background(image: np.ndarray, box: Box) -> tuple[int, int, int]:
    """Cor predominante na borda em volta do texto (fundo do balão)."""
    x0, y0, x1, y1 = box
    patch = image[y0:y1, x0:x1]
    if patch.size == 0:
        return 255, 255, 255
    border = np.concatenate([patch[0], patch[-1], patch[:, 0], patch[:, -1]])
    return tuple(int(v) for v in np.median(border, axis=0))


@dataclass
class ScreenResult:
    items: list[OverlayItem]  # traduções
    color_box: Box | None  # onde está a página na tela (quando colorida)
    color_image: Image.Image | None  # a página colorida, do tamanho de color_box


def _reading_order(box: Box, page_height: int, source: str) -> tuple:
    """Ordem aproximada de leitura: faixas de cima para baixo; dentro da faixa, da direita para a
    esquerda no mangá japonês e da esquerda para a direita nos demais."""
    band = round(box[1] / max(1, page_height * 0.08))
    center = (box[0] + box[2]) / 2
    return band, -center if source == "ja" else center


class _Cache:
    """Traduções já feitas, reconhecidas pela aparência do recorte (tolera pequenas diferenças de captura)."""

    def __init__(self, limit: int = 3000):
        self._entries: deque[tuple[tuple, int, Result]] = deque(maxlen=limit)

    def get(self, key: tuple, signature: int) -> Result | None:
        for entry_key, entry_sig, result in reversed(self._entries):
            if entry_key == key and (entry_sig ^ signature).bit_count() <= 12:
                return result
        return None

    def put(self, key: tuple, signature: int, result: Result) -> None:
        self._entries.append((key, signature, result))

    def clear(self) -> None:
        self._entries.clear()


class Pipeline:
    def __init__(self, db: Database | None = None):
        # Sem banco (ex.: --image), as traduções ficam só na memória desta execução
        self._db = db
        self._work: int | None = None
        # Uma coisa por vez na GPU; o atalho passa na frente da importação (ver _interactive/_background)
        self._gpu = threading.Lock()
        self._waiting_lock = threading.Lock()
        self._waiting = 0
        self._device: str | None = None
        self._detector: Detector | None = None
        self._ocr: OcrEngine | None = None
        self._nllb: translators.NllbTranslator | None = None
        self._colorizer: Colorizer | None = None
        self._cache = _Cache()
        # Falas das últimas telas: dão contexto de cena para os LLMs
        self._context: deque[str] = deque(maxlen=30)

    def clear_cache(self, work_id: int | None = None, forget_saved: bool = False) -> int:
        """Esvazia o cache em memória; com forget_saved, apaga também as traduções salvas da obra."""
        with self._interactive():
            return self._clear_cache(work_id, forget_saved)

    def _clear_cache(self, work_id: int | None, forget_saved: bool) -> int:
        self._cache.clear()
        self._context.clear()
        if forget_saved and self._db is not None:
            return self._db.forget_translations(work_id)
        return 0

    def _prepare(self, config: Config, status: Callable[[str], None]) -> None:
        import torch

        device = "cuda" if config.use_gpu and torch.cuda.is_available() else "cpu"
        if device != self._device:
            self._device = device
            self._detector = self._ocr = self._nllb = self._colorizer = None
        if self._detector is None:
            status("Carregando o detector de balões (na primeira vez ele é baixado)…")
            self._detector = Detector(device)
            self._ocr = OcrEngine(device)

    def warm_up(self, config: Config) -> None:
        """Carrega os modelos locais antes do primeiro atalho (na primeira vez eles são baixados)."""
        with self._interactive():
            self._warm_up(config)

    def _warm_up(self, config: Config) -> None:
        self._prepare(config, lambda _msg: None)
        if config.source_lang != AUTO:
            self._ocr.load(config.source_lang)  # também no modo visão: o texto lido é a chave do banco
        if config.engine == "local" and self._nllb is None:
            self._nllb = translators.NllbTranslator(self._device)
        if config.colorize and self._colorizer is None:
            self._colorizer = Colorizer(self._device)

    def process(
        self,
        image: Image.Image,
        config: Config,
        status: Callable[[str], None] = lambda _msg: None,
        translate: bool = True,
        colorize: bool = False,
    ) -> ScreenResult:
        with self._interactive():
            return self._process(image, config, status, translate, colorize)

    def _process(
        self,
        image: Image.Image,
        config: Config,
        status: Callable[[str], None],
        translate: bool,
        colorize: bool,
    ) -> ScreenResult:
        vision = config.engine in VISION_ENGINES
        if translate and config.source_lang == AUTO and not vision:
            raise PipelineError(
                "Para usar 'Detectar idioma', escolha um motor em que o LLM lê a imagem. "
                "Com OCR local, defina o idioma de origem no menu da bandeja."
            )
        self._prepare(config, status)
        if config.current_work != self._work:
            # Outra obra: as falas recentes da anterior não servem de contexto
            self._work = config.current_work
            self._context.clear()
        detection = self._detector.detect(image, config.include_free_text)
        result = ScreenResult(items=[], color_box=None, color_image=None)

        if colorize:
            result.color_box = find_page(image, detection.bubbles)
            if result.color_box is not None:
                if self._colorizer is None:
                    status("Carregando o colorizador (na primeira vez ele é baixado, ~120 MB)…")
                    self._colorizer = Colorizer(self._device)
                result.color_image = self._colorizer.colorize(image.crop(result.color_box))

        if translate and detection.regions:
            # Com a página colorida, o fundo que cobre o texto original acompanha a cor nova do balão
            base = image
            if result.color_image is not None:
                base = image.copy()
                base.paste(result.color_image, result.color_box[:2])
            result.items = self._translate_regions(image, np.asarray(base.convert("RGB")), detection.regions, config, status)
        return result

    def _translate_regions(
        self,
        image: Image.Image,
        frame: np.ndarray,
        regions: list[Region],
        config: Config,
        status: Callable[[str], None],
    ) -> list[OverlayItem]:
        key = (config.current_work, config.engine, config.openai_model, config.claude_model, config.source_lang, config.target_lang)
        crops = [image.crop(r.text_box) for r in regions]
        signatures = [_dhash(c) for c in crops]
        results: dict[int, Result] = {}
        missing: list[int] = []
        for index, signature in enumerate(signatures):
            cached = self._cache.get(key, signature)
            if cached is not None:
                results[index] = cached
            else:
                missing.append(index)

        if missing:
            new = self._translate(image, regions, crops, missing, config, status)
            for index in missing:
                if index in new:
                    results[index] = new[index]
                    self._cache.put(key, signatures[index], new[index])

        items = []
        for index, region in enumerate(regions):
            result = results.get(index)
            # Tradução igual ao original: já estava no idioma de destino (ex.: botões de um site em português)
            if result is None or not result.translation or result.translation.casefold() == result.original.casefold():
                continue
            fill = _expand(region.text_box, image.size)
            items.append(
                OverlayItem(
                    fill=fill,
                    area=region.area,
                    text=result.translation,
                    original=result.original,
                    background=_background(frame, fill),
                    bubble=region.bubble,
                )
            )
        return items

    def _translate(
        self,
        image: Image.Image,
        regions: list[Region],
        crops: list[Image.Image],
        indices: list[int],
        config: Config,
        status: Callable[[str], None],
    ) -> dict[int, Result]:
        """Tradução das regiões `indices`: banco primeiro (pelo texto lido), tradutor só para o que falta."""
        source = config.source_lang
        if source == AUTO:
            # Sem idioma definido não há OCR local, logo nem chave para o banco: o LLM lê a imagem direto.
            return self._call_translator(image, regions, [(i, "") for i in indices], config)

        status("Lendo os textos…")
        readable = self._read(crops, indices, source)
        if not readable:
            return {}

        results: dict[int, Result] = {}
        pending = readable
        key = self._db_key(config)
        if self._db is not None:
            saved = self._db.find_translations(key, [text for _i, text in readable])
            pending = []
            for index, text in readable:
                translation = saved.get(normalize(text))
                if translation is not None:
                    results[index] = Result(0, text, translation)
                else:
                    pending.append((index, text))

        if pending:
            new = self._call_translator(image, regions, pending, config, status)
            results.update(new)
            local_text = dict(pending)
            if self._db is not None:
                # Gravado pelo texto do OCR local (no modo visão, o LLM pode transcrever um pouco diferente)
                self._db.save_translations(key, [(local_text[i], r.translation) for i, r in new.items()])
            self._context.extend(r.original for _i, r in sorted(new.items()) if r.original)
        return results

    def _read(self, crops: list[Image.Image], indices: list[int], source: str) -> list[tuple[int, str]]:
        """OCR das regiões `indices`, descartando leituras incertas ou sem a escrita do idioma de origem
        (lixo, textos da interface do sistema). Retorna (índice, texto) na mesma ordem."""
        if self._ocr is None:
            raise PipelineError("OCR não carregado.")
        readings = self._ocr.read([crops[i] for i in indices], source)
        return [
            (i, text)
            for i, (text, confidence) in zip(indices, readings)
            if confidence >= 0.3 and looks_like(text, source) and not (source == "ja" and self._ocr.is_latin(crops[i]))
        ]

    def read_page(self, image: Image.Image, config: Config) -> list[tuple[Box, str]]:
        """Detecção + OCR de uma página importada: (caixa do texto, texto lido) na ordem de leitura.

        Roda em segundo plano e cede a GPU a cada página se o usuário pedir uma tradução na tela.
        """
        with self._background():
            self._prepare(config, lambda _msg: None)
            regions = self._detector.detect(image, config.include_free_text).regions
            crops = [image.crop(r.text_box) for r in regions]
            readable = self._read(crops, list(range(len(regions))), config.source_lang)
        boxes = [(regions[i].text_box, text) for i, text in readable]
        return sorted(boxes, key=lambda item: _reading_order(item[0], image.height, config.source_lang))

    @contextmanager
    def _interactive(self):
        """Uso da GPU pelo atalho (tem prioridade sobre a importação)."""
        with self._waiting_lock:
            self._waiting += 1
        self._gpu.acquire()
        with self._waiting_lock:
            self._waiting -= 1
        try:
            yield
        finally:
            self._gpu.release()

    @contextmanager
    def _background(self):
        """Uso da GPU pela importação: espera enquanto houver um pedido do atalho na fila."""
        while True:
            with self._waiting_lock:
                idle = self._waiting == 0
            if idle and self._gpu.acquire(timeout=0.05):
                break
            time.sleep(0.02)
        try:
            yield
        finally:
            self._gpu.release()

    def _call_translator(
        self,
        image: Image.Image,
        regions: list[Region],
        lines_by_index: list[tuple[int, str]],
        config: Config,
        status: Callable[[str], None] = lambda _msg: None,
    ) -> dict[int, Result]:
        """Chama o motor configurado para (índice da região, texto lido). Retorna índice -> resultado."""
        source, target, engine = config.source_lang, config.target_lang, config.engine
        context = list(self._context)
        # Lista de personagens da obra: nomes e gênero consistentes (só os motores com LLM a usam)
        characters = self._db.characters(config.current_work) if self._db is not None else []
        # Números a partir de 1 (no modo visão, são os que aparecem desenhados na imagem enviada)
        lines = [Line(n, text) for n, (_i, text) in enumerate(lines_by_index, start=1)]

        if engine in VISION_ENGINES:
            page = translators.marked_page(image, {line.id: regions[i].text_box for line, (i, _t) in zip(lines, lines_by_index)})
            if engine == "openai-vision":
                out = llm_openai.translate_image(
                    page, lines, context, source, target, config.openai_model, self._key(credentials.OPENAI), characters
                )
            else:
                out = llm_anthropic.translate_image(
                    page, lines, context, source, target, config.claude_model, self._key(credentials.ANTHROPIC), characters
                )
        elif engine == "google":
            out = translators.translate_google(lines, source, target)
        elif engine == "local":
            if self._nllb is None:
                status("Carregando o tradutor offline (na primeira vez ele é baixado, ~2,5 GB)…")
                self._nllb = translators.NllbTranslator(self._device)
            out = self._nllb.translate(lines, source, target)
        elif engine == "openai-text":
            out = llm_openai.translate_text(
                lines, context, source, target, config.openai_model, self._key(credentials.OPENAI), characters
            )
        else:
            out = llm_anthropic.translate_text(
                lines, context, source, target, config.claude_model, self._key(credentials.ANTHROPIC), characters
            )
        return {lines_by_index[r.id - 1][0]: r for r in out}

    @staticmethod
    def _db_key(config: Config) -> TranslationKey:
        if config.engine.startswith("openai"):
            model = config.openai_model
        elif config.engine.startswith("claude"):
            model = config.claude_model
        elif config.engine == "local":
            model = translators.NLLB_ID
        else:
            model = config.engine
        return TranslationKey(config.current_work, config.source_lang, config.target_lang, config.engine, model)

    @staticmethod
    def _key(name: str) -> str:
        key = credentials.get_key(name)
        if not key:
            service = "OpenAI" if name == credentials.OPENAI else "Claude (Anthropic)"
            raise PipelineError(f"Chave da API da {service} não configurada. Abra Configurações no ícone da bandeja.")
        return key
