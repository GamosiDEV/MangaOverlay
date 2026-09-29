"""Do print da tela até os itens desenhados na sobreposição. Roda fora da thread da interface."""

import threading
import time
from collections import defaultdict, deque
from difflib import SequenceMatcher
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from PIL import Image

from . import credentials, llm_anthropic, llm_openai, translators
from .colorize import Colorizer, find_page
from .config import ENGINES, VISION_ENGINES, Config
from .db import FUZZY_MIN_RATIO, FUZZY_RATIO_MIN_LENGTH, Database, TranslationKey, _one_edit_apart, match_key, normalize
from .memory import GLOSSARY_LIMIT, SCREEN_CONSOLIDATE_EVERY
from .detector import Box, Detector, Region
from .languages import AUTO, looks_like
from .ocr import OcrEngine
from .translators import Line, Result


ALL_ENGINES = tuple(ENGINES)
# Confiança mínima do detector para tentar ler em japonês uma caixa que o leitor do idioma da obra não leu
_SECOND_LANGUAGE_MIN_SCORE = 0.45


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
    missing: bool = False  # modo "só traduções salvas": fala sem tradução no banco (original fica visível, marcado)


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


def _same_line(a: str, b: str) -> bool:
    """Mesma fala lida de forma um pouco diferente (mesmas regras da busca tolerante do banco)."""
    if _one_edit_apart(a, b):
        return True
    return len(a) >= FUZZY_RATIO_MIN_LENGTH and SequenceMatcher(None, a, b, autojunk=False).ratio() >= FUZZY_MIN_RATIO


def _fit_transform(pairs: list[tuple[tuple[float, float], Box]], size: tuple[int, int]) -> tuple[float, float, float] | None:
    """Escala e deslocamento que levam as caixas do arquivo às posições na tela. None se as falas não concordarem."""
    if len(pairs) < 2:
        return None
    files = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for _c, b in pairs]
    screens = [c for c, _b in pairs]
    ratios = []
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            file_dist = ((files[i][0] - files[j][0]) ** 2 + (files[i][1] - files[j][1]) ** 2) ** 0.5
            screen_dist = ((screens[i][0] - screens[j][0]) ** 2 + (screens[i][1] - screens[j][1]) ** 2) ** 0.5
            if file_dist > 40:
                ratios.append(screen_dist / file_dist)
    if not ratios:
        return None
    scale = float(np.median(ratios))
    dx = float(np.median([s[0] - f[0] * scale for s, f in zip(screens, files)]))
    dy = float(np.median([s[1] - f[1] * scale for s, f in zip(screens, files)]))
    # Conferência: as falas reconhecidas têm de cair perto de onde a projeção diz
    tolerance = 0.03 * max(size)
    agree = sum(1 for s, f in zip(screens, files) if abs(f[0] * scale + dx - s[0]) < tolerance and abs(f[1] * scale + dy - s[1]) < tolerance)
    if scale <= 0.05 or agree < 2 or agree < 0.6 * len(pairs):
        return None
    return scale, dx, dy


def _center_inside(box: Box, area: Box) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return area[0] <= cx <= area[2] and area[1] <= cy <= area[3]


def _overlaps(a: Box, b: Box) -> bool:
    """A mesma caixa vista pelos dois perfis: o centro de uma cai dentro da outra."""
    return _center_inside(a, b) or _center_inside(b, a)


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

        regions, verify_only = detection.regions, frozenset()
        if translate and self._db is not None and config.current_work is not None and config.source_lang != AUTO:
            regions, verify_only = self._with_saved_extras(image, detection, config)
        if translate and regions:
            # Com a página colorida, o fundo que cobre o texto original acompanha a cor nova do balão
            base = image
            if result.color_image is not None:
                base = image.copy()
                base.paste(result.color_image, result.color_box[:2])
            frame = np.asarray(base.convert("RGB"))
            result.items = self._translate_regions(image, frame, regions, config, status, verify_only)
            if self._db is not None and config.current_work is not None and config.source_lang != AUTO:
                extra = self._anchor_page(image, frame, result.items, config, status)
                # A âncora sabe a tradução de caixas que a tela detectou mas leu errado: substitui a marcação de falta
                kept = [i for i in result.items if not (i.missing and any(_overlaps(i.fill, e.fill) for e in extra))]
                result.items = kept + extra
        return result

    def _anchor_page(
        self, image: Image.Image, frame: np.ndarray, items: list[OverlayItem], config: Config, status: Callable[[str], None]
    ) -> list[OverlayItem]:
        """Se a tela mostra uma página importada, desenha TODAS as falas dela a partir do banco, inclusive as que a
        captura da tela não detectou ou não leu (legendas sobre o desenho, textos pequenos).

        As falas reconhecidas votam na página; com 2 ou mais concordando, calcula a escala e a posição da página na
        tela e projeta as outras falas. Sem concordância, não faz nada (nunca desenha no lugar errado)."""
        regions = self._db.work_regions(config.current_work)
        if not regions or not items:
            return []
        by_key: dict[str, list[tuple[int, Box]]] = defaultdict(list)
        for page, box, text in regions:
            by_key[match_key(text)].append((page, box))
        votes: dict[int, list[tuple[tuple[float, float], Box]]] = defaultdict(list)
        for item in items:
            if item.missing:
                continue
            wanted = match_key(item.original)
            if len(wanted) < 4:
                continue
            matches = by_key.get(wanted) or [
                entry
                for key, entries in by_key.items()
                if abs(len(key) - len(wanted)) <= max(2, len(wanted) // 3) and _same_line(wanted, key)
                for entry in entries
            ]
            if not matches or len({page for page, _b in matches}) > 3:
                continue  # fala comum demais (aparece em várias páginas): não identifica a página
            center = ((item.fill[0] + item.fill[2]) / 2, (item.fill[1] + item.fill[3]) / 2)
            for page, box in matches:
                votes[page].append((center, box))
        if not votes:
            return []
        page, pairs = max(votes.items(), key=lambda kv: len(kv[1]))
        transform = _fit_transform(pairs, image.size)
        if transform is None:
            return []
        scale, dx, dy = transform

        shown = [item.fill for item in items if not item.missing]
        projected = []
        for region_page, box, text in regions:
            if region_page != page:
                continue
            screen_box = (
                round(box[0] * scale + dx), round(box[1] * scale + dy), round(box[2] * scale + dx), round(box[3] * scale + dy)
            )
            if screen_box[2] <= 0 or screen_box[3] <= 0 or screen_box[0] >= image.width or screen_box[1] >= image.height:
                continue  # parte da página fora da tela
            if any(_overlaps(screen_box, s) for s in shown):
                continue  # já está na tela
            projected.append((screen_box, text))
        if not projected:
            return []

        key = self._db_key(config)
        saved = self._db.find_translations(key, [t for _b, t in projected], also_engines=ALL_ENGINES)
        results = {i: saved.get(normalize(t)) for i, (_b, t) in enumerate(projected)}
        pending = [(i, t) for i, (_b, t) in enumerate(projected) if results[i] is None]
        if pending and not config.saved_only:
            # Texto da página importada (não de menus): pode ir ao tradutor
            fake = [Region(text_box=b, area=b, bubble=None) for b, _t in projected]
            new = self._call_translator(image, fake, pending, config, status)
            self._db.save_translations(key, [(dict(pending)[i], r.translation or dict(pending)[i]) for i, r in new.items()])
            results.update({i: r.translation for i, r in new.items()})

        extra = []
        for i, (box, text) in enumerate(projected):
            translation = results.get(i)
            fill = _expand(box, image.size)
            if not translation:
                if config.saved_only:
                    extra.append(OverlayItem(fill, box, "", text, (255, 255, 255), None, missing=True))
                continue
            if translation.casefold() == text.casefold():
                continue  # "não precisa traduzir" (reticências, onomatopeias)
            extra.append(OverlayItem(fill, _expand(box, image.size), translation, text, _background(frame, fill), None))
        return extra

    def _with_saved_extras(self, image: Image.Image, detection, config: Config) -> tuple[list[Region], frozenset[int]]:
        """Na tela, os filtros de segurança deixam de fora legendas, narração e textos soltos. Dentro da área da página,
        esses textos também são procurados, mas só aparecem se já tiverem tradução salva (vinda de um capítulo
        importado): nunca são enviados ao tradutor, então textos de menus e sites continuam protegidos."""
        page = find_page(image, detection.bubbles)
        if page is None:
            return detection.regions, frozenset()
        known = detection.regions
        extras = [
            r
            for r in self._detector.detect(image, profile="file").regions
            if _center_inside(r.text_box, page) and not any(_overlaps(r.text_box, k.text_box) for k in known)
        ]
        return known + extras, frozenset(range(len(known), len(known) + len(extras)))

    def _translate_regions(
        self,
        image: Image.Image,
        frame: np.ndarray,
        regions: list[Region],
        config: Config,
        status: Callable[[str], None],
        verify_only: frozenset[int] = frozenset(),
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
            new = self._translate(image, regions, crops, missing, config, status, verify_only)
            for index in missing:
                if index in new:
                    results[index] = new[index]
                    # "Sem tradução salva" não vai para o cache: ao desligar o modo, a fala precisa ser traduzida
                    if new[index].translation:
                        self._cache.put(key, signatures[index], new[index])

        items = []
        for index, region in enumerate(regions):
            result = results.get(index)
            if config.saved_only and result is not None and not result.translation:
                box = _expand(region.text_box, image.size)
                items.append(OverlayItem(box, region.area, "", result.original, (255, 255, 255), region.bubble, missing=True))
                continue
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
        verify_only: frozenset[int] = frozenset(),
    ) -> dict[int, Result]:
        """Tradução das regiões `indices`: banco primeiro (pelo texto lido), tradutor só para o que falta.
        Regiões em `verify_only` só aparecem se estiverem no banco (nunca vão ao tradutor)."""
        source = config.source_lang
        if source == AUTO:
            if config.saved_only:
                raise PipelineError(
                    "O modo “só traduções salvas” precisa do idioma de origem definido (com “Detectar” não dá para "
                    "procurar no banco). Escolha a origem no menu da bandeja."
                )
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
            # Se o motor atual ainda não traduziu, aceita qualquer tradução já paga com IA (ex.: lote feito com
            # outro modelo, ou leitura no modo visão/NLLB de uma obra traduzida em lote no modo texto)
            # No modo "só traduções salvas" vale qualquer tradução guardada da obra, de qualquer motor
            engines = ALL_ENGINES if config.saved_only else ("openai-text", "claude-text")
            saved = self._db.find_translations(key, [text for _i, text in readable], also_engines=engines)
            pending = []
            for index, text in readable:
                translation = saved.get(normalize(text))
                if translation is not None:
                    results[index] = Result(0, text, translation)
                else:
                    pending.append((index, text))

        pending = [(index, text) for index, text in pending if index not in verify_only]
        if pending and config.saved_only:
            # Nunca traduz: o que não está no banco volta sem tradução (e é marcado na tela)
            results.update({index: Result(0, text, "") for index, text in pending})
            return results
        if pending:
            new = self._call_translator(image, regions, pending, config, status)
            results.update(new)
            local_text = dict(pending)
            if self._db is not None:
                # Gravado pelo texto do OCR local (no modo visão, o LLM pode transcrever um pouco diferente)
                self._db.save_translations(key, [(local_text[i], r.translation) for i, r in new.items()])
            self._context.extend(r.original for _i, r in sorted(new.items()) if r.original)
        return results

    def _read(
        self, crops: list[Image.Image], indices: list[int], source: str, screen: bool = True, scores: list[float] | None = None
    ) -> list[tuple[int, str]]:
        """OCR das regiões `indices`, descartando leituras incertas ou sem a escrita do idioma de origem
        (lixo, textos da interface do sistema). Retorna (índice, texto) na mesma ordem.

        `screen=False` (arquivo importado): sem o filtro de texto latino, que existe para botões e legendas de
        sites; num arquivo ele só descartaria balões japoneses com palavras em inglês."""
        if self._ocr is None:
            raise PipelineError("OCR não carregado.")
        readings = self._ocr.read([crops[i] for i in indices], source)
        accepted = {
            i: text
            for i, (text, confidence) in zip(indices, readings)
            if confidence >= 0.3
            and looks_like(text, source)
            and not (screen and source == "ja" and self._ocr.is_latin(crops[i]))
        }
        if not screen and source != "ja" and scores is not None:
            # Edições traduzidas costumam deixar em japonês placas, bilhetes e textos do cenário. O leitor do
            # idioma da obra não lê nada ali; o leitor de mangá japonês tenta, mas só em caixas que o detector
            # marcou com confiança (ele "inventa" japonês a partir de desenhos) e só se sair escrita japonesa.
            retry = [i for i in indices if i not in accepted and scores[i] >= _SECOND_LANGUAGE_MIN_SCORE]
            for i, (text, _confidence) in zip(retry, self._ocr.read([crops[i] for i in retry], "ja")):
                if len(text) >= 2 and looks_like(text, "ja"):
                    accepted[i] = text
        return [(i, accepted[i]) for i in indices if i in accepted]

    def read_page(self, image: Image.Image, config: Config) -> list[tuple[Box, str]]:
        """Detecção + OCR de uma página importada: (caixa do texto, texto lido) na ordem de leitura.

        Roda em segundo plano e cede a GPU a cada página se o usuário pedir uma tradução na tela.
        """
        with self._background():
            self._prepare(config, lambda _msg: None)
            regions = self._detector.detect(image, profile="file").regions
            crops = [image.crop(r.text_box) for r in regions]
            readable = self._read(
                crops, list(range(len(regions))), config.source_lang, screen=False, scores=[r.score for r in regions]
            )
            # Varredura da página inteira: linhas de texto que o detector de balões não pegou (legendas sobre o
            # desenho, placas, onomatopeias em letras). Japonês vertical fica com o detector de mangá, que é melhor.
            if config.source_lang != "ja":
                found = [regions[i].text_box for i, _t in readable]
                sweep = [
                    b
                    for b in self._ocr.find_text_blocks(image, config.source_lang)
                    if not any(_overlaps(b, f) for f in found) and (b[2] - b[0]) * (b[3] - b[1]) >= 150
                ]
                for box, (text, confidence) in zip(sweep, self._ocr.read([image.crop(b) for b in sweep], config.source_lang)):
                    if confidence >= 0.4 and looks_like(text, config.source_lang) and sum(ch.isalpha() for ch in text) >= 2:
                        regions.append(Region(text_box=box, area=box, bubble=None, score=0.5))
                        readable.append((len(regions) - 1, text))
        boxes = [(regions[i].text_box, text) for i, text in readable]
        return sorted(boxes, key=lambda item: _reading_order(item[0], image.height, config.source_lang))

    def translate_offline(self, lines: list[Line], config: Config) -> list[Result]:
        """NLLB na GPU para a tradução em lote (em segundo plano: cede a vez ao atalho)."""
        with self._background():
            self._prepare(config, lambda _msg: None)
            if self._nllb is None:
                self._nllb = translators.NllbTranslator(self._device)
            return self._nllb.translate(lines, config.source_lang, config.target_lang)

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
        # Lista de personagens e memória da obra: nomes, termos e contexto consistentes (só os motores com LLM usam)
        characters = self._db.characters(config.current_work) if self._db is not None else []
        memory = self._db.memory(config.current_work) if self._db is not None else None
        terms = []
        # Números a partir de 1 (no modo visão, são os que aparecem desenhados na imagem enviada)
        lines = [Line(n, text) for n, (_i, text) in enumerate(lines_by_index, start=1)]

        if engine in VISION_ENGINES:
            page = translators.marked_page(image, {line.id: regions[i].text_box for line, (i, _t) in zip(lines, lines_by_index)})
            if engine == "openai-vision":
                out, _usage, terms = llm_openai.translate_image(
                    page, lines, context, source, target, config.openai_model, self._key(credentials.OPENAI), characters, memory, config.current_work
                )
            else:
                out, _usage, terms = llm_anthropic.translate_image(
                    page, lines, context, source, target, config.claude_model, self._key(credentials.ANTHROPIC), characters, memory
                )
        elif engine == "google":
            out = translators.translate_google(lines, source, target)
        elif engine == "local":
            if self._nllb is None:
                status("Carregando o tradutor offline (na primeira vez ele é baixado, ~2,5 GB)…")
                self._nllb = translators.NllbTranslator(self._device)
            out = self._nllb.translate(lines, source, target)
        elif engine == "openai-text":
            out, _usage, terms = llm_openai.translate_text(
                lines, context, source, target, config.openai_model, self._key(credentials.OPENAI), characters, memory, config.current_work
            )
        else:
            out, _usage, terms = llm_anthropic.translate_text(
                lines, context, source, target, config.claude_model, self._key(credentials.ANTHROPIC), characters, memory
            )
        if terms and self._db is not None and config.current_work is not None:
            self._db.add_terms(config.current_work, terms)
            # A foto da memória só muda de vez em quando, para não quebrar o cache de prompt a cada página
            if self._db.pending_terms(config.current_work) >= SCREEN_CONSOLIDATE_EVERY:
                self._db.consolidate_terms(config.current_work, GLOSSARY_LIMIT)
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
