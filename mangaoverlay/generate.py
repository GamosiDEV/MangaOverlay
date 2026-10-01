"""Capítulos traduzidos em imagem: cada página importada ganha as traduções salvas desenhadas por cima, e o capítulo
vira um CBZ (abre direto no Mihon/Tachiyomi, Perfect Viewer e outros leitores) ou uma pasta de imagens.

Nada é traduzido de novo: vale qualquer tradução salva da obra (de qualquer motor, com preferência pelo atual). As
falas que faltarem só vão ao tradutor se o usuário pedir (a janela sempre oferece, com o custo antes).
O detector roda outra vez em cada página só para achar o balão de cada fala: o banco guarda apenas a caixa do texto,
e sem o balão o desenho apagaria o contorno dele.
"""

import io
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
from PIL import Image
from PySide6.QtCore import QObject, Signal

from . import pricing
from .config import VISION_ENGINES, Config
from .db import Database, StoredPage, TranslationKey, Work, normalize
from .detector import Region
from .pipeline import (
    ALL_ENGINES,
    OverlayItem,
    Pipeline,
    PipelineError,
    _expand,
    box_paper,
    bubble_shape,
    over_art,
    same_place,
    text_background,
)
from .sources import load_page
from .translators import TranslationError, llm_instructions

FORMATS = ("cbz", "pasta")
# 88: ~15% menor que 92 sem diferença visível; a retícula dos mangás comprime mal de qualquer jeito
JPEG_QUALITY = 88
LLM_ENGINES = ("openai-text", "openai-vision", "claude-text", "claude-vision")
# Página enviada no modo visão (lado maior de até 1800 px): ordem de grandeza dos tokens da imagem
_IMAGE_TOKENS = 1500
# Caracteres que o Windows não aceita em nomes de arquivo
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def offscreen_qt() -> None:
    """Qt sem janela, para desenhar em imagens na linha de comando. No Windows, a plataforma "offscreen" não enxerga as
    fontes do sistema (só procura na pasta do próprio Qt, vazia): a fonte escolhida nas Configurações seria ignorada."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if sys.platform == "win32":
        os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))


def work_config(config: Config, work: Work) -> Config:
    """A configuração atual aplicada à obra: o idioma de origem é sempre o dela."""
    return replace(config, current_work=work.id, source_lang=work.source_lang)


def saved_translations(db: Database, key: TranslationKey, texts: list[str]) -> dict[str, str]:
    """Tradução salva de cada texto (indexada pelo texto normalizado): a do motor atual e, se faltar, a de qualquer outro.

    Se ainda faltar, vale também a tradução feita quando a obra estava com outro idioma de origem (o usuário trocou o
    idioma depois de traduzir): o texto lido é o mesmo, só a chave mudou."""
    if not texts:
        return {}
    found = db.find_translations(key, texts, also_engines=ALL_ENGINES)
    for source in db.translation_sources(key.work_id, key.target):
        missing = [t for t in texts if normalize(t) and normalize(t) not in found]
        if not missing:
            break
        if source != key.source:
            found.update(db.find_translations(replace(key, source=source), missing, also_engines=ALL_ENGINES))
    return found


# --- falas sem tradução ---------------------------------------------------------------


@dataclass
class Missing:
    """Falas sem tradução salva nos capítulos escolhidos (repetidas contam uma vez) e o custo de traduzi-las."""

    lines: int
    pages: int
    cost: float | None  # None: modelo sem preço conhecido; 0: motor gratuito
    unread_pages: int  # páginas ainda não lidas pelo OCR: saem como no original


def missing_by_chapter(db: Database, config: Config, work: Work) -> dict[int, list[tuple[int, str]]]:
    """Por capítulo da obra: (página, texto) de cada fala sem tradução salva."""
    key = Pipeline._db_key(work_config(config, work))
    lines = db.chapter_lines([c.id for c in db.chapters(work.id)])
    saved = saved_translations(db, key, [t for items in lines.values() for _p, t in items])
    return {c: [(p, t) for p, t in items if normalize(t) and normalize(t) not in saved] for c, items in lines.items()}


def estimate_missing(
    db: Database, config: Config, work: Work, missing: dict[int, list[tuple[int, str]]], chapter_ids: list[int]
) -> Missing:
    by_page: dict[int, list[str]] = {}
    seen: set[str] = set()
    for chapter in chapter_ids:
        for page, text in missing.get(chapter, []):
            if normalize(text) not in seen:
                seen.add(normalize(text))
                by_page.setdefault(page, []).append(text)
    texts = [t for items in by_page.values() for t in items]
    cost: float | None = 0.0
    if config.engine in LLM_ENGINES and texts:
        key = Pipeline._db_key(work_config(config, work))
        vision = config.engine in VISION_ENGINES
        instructions = llm_instructions(work.source_lang, key.target, vision, db.characters(work.id), db.memory(work.id))
        # Um pedido por página com falas faltando (a tradução é feita página a página, na hora de desenhar)
        fixed = pricing.estimate_tokens(instructions) + (_IMAGE_TOKENS if vision else 0)
        input_tokens = len(by_page) * fixed + sum(pricing.estimate_tokens(t) + 10 for t in texts)
        output_tokens = sum(round(pricing.estimate_tokens(t) * 1.3) + 12 for t in texts)
        cost = pricing.cost(key.model, input_tokens, output_tokens)
    unread = sum(c.pages - c.read for c in db.chapters(work.id) if c.id in chapter_ids)
    return Missing(len(texts), len(by_page), cost, unread)


# --- desenho e gravação -----------------------------------------------------------------


def render_page(
    image: Image.Image,
    regions: list[Region],
    texts: list[str],
    translations: dict[str, str],
    font,
    language: str = "",
    erased: frozenset[int] = frozenset(),
) -> tuple[Image.Image, int]:
    """A página com as traduções desenhadas (mesmo desenho do overlay) e quantas falas foram desenhadas. `erased`:
    falas cujo texto original já foi apagado (desenho reconstruído): a tradução vai no lugar dele, sem cobertura e com o
    contorno de legibilidade dos textos soltos."""
    from PySide6.QtGui import QImage, QPainter

    from .render import paint_items
    from .screenshot import pil_to_qimage, qimage_to_pil

    frame = np.asarray(image.convert("RGB"))
    items = []
    placed: list = []
    for index, (region, text) in enumerate(zip(regions, texts)):
        translation = translations.get(normalize(text))
        # Tradução igual ao original: "não precisa traduzir" (reticências, onomatopeias)
        if not translation or translation.casefold() == text.casefold():
            continue
        # A mesma fala marcada duas vezes: uma tradução por cima da outra (sem cobertura, as duas apareceriam)
        if any(same_place(region.text_box, other) for other in placed):
            continue
        placed.append(region.text_box)
        fill = _expand(region.text_box, image.size)
        background = text_background(frame, region.text_box, fill)
        if index in erased:
            items.append(OverlayItem(fill, fill, translation, text, background, None, language=language, cover=False))
            continue
        shape = bubble_shape(frame, region.bubble, background) if region.bubble else "ellipse"
        if shape == "rect":
            background = box_paper(frame, region.bubble)
        items.append(OverlayItem(fill, region.area, translation, text, background, region.bubble, shape=shape, language=language))
    if not items:
        return image, 0
    canvas = pil_to_qimage(image).convertToFormat(QImage.Format.Format_RGB32)
    painter = QPainter(canvas)
    paint_items(painter, items, font)
    painter.end()
    return qimage_to_pil(canvas), len(items)


def encode_page(image: Image.Image) -> bytes:
    arr = np.asarray(image.convert("RGB"))
    if (arr[..., 0] == arr[..., 1]).all() and (arr[..., 1] == arr[..., 2]).all():
        image = image.convert("L")  # página em preto e branco: o arquivo fica bem menor
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def output_path(folder: Path, work_name: str, chapter_name: str, fmt: str) -> Path:
    """`<obra> - <capítulo> (traduzido)`, com .cbz ou como pasta. O nome da obra não se repete se o capítulo já o tiver."""
    name = chapter_name if work_name.casefold() in chapter_name.casefold() else f"{work_name} - {chapter_name}"
    name = _INVALID.sub("_", name).strip(" .") or "capitulo"
    name += " (traduzido)"
    return folder / (name + ".cbz" if fmt == "cbz" else name)


def comic_info(work_name: str, chapter_name: str, order: float, target_lang: str, pages: int) -> str:
    """ComicInfo.xml: os leitores (Mihon, Komga, Kavita…) mostram obra e capítulo em vez do nome do arquivo."""
    number = f"  <Number>{order:g}</Number>\n" if order else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<ComicInfo>\n'
        f"  <Series>{escape(work_name)}</Series>\n  <Title>{escape(chapter_name)}</Title>\n{number}"
        f"  <LanguageISO>{escape(target_lang)}</LanguageISO>\n  <PageCount>{pages}</PageCount>\n</ComicInfo>\n"
    )


class _Output:
    """Grava num arquivo ou pasta temporário e só troca pelo definitivo no fim: parar ou falhar no meio nunca deixa um
    capítulo pela metade, nem apaga a versão gerada antes."""

    def __init__(self, target: Path, fmt: str):
        self.target = target
        self.temp = target.with_name(target.name + ".parcial")
        self._zip: zipfile.ZipFile | None = None
        self._remove(self.temp)
        target.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "cbz":
            # Sem compressão: as páginas já são JPG
            self._zip = zipfile.ZipFile(self.temp, "w", zipfile.ZIP_STORED)
        else:
            self.temp.mkdir()

    def add(self, name: str, data: bytes) -> None:
        if self._zip is not None:
            self._zip.writestr(name, data)
        else:
            (self.temp / name).write_bytes(data)

    def commit(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None
        self._remove(self.target)
        os.replace(self.temp, self.target)

    def discard(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None
        self._remove(self.temp)

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


@dataclass
class ChapterResult:
    path: Path | None  # None: nada gravado (parado antes de terminar, ou o original do capítulo sumiu)
    pages: int = 0
    drawn: int = 0  # falas desenhadas
    translated: int = 0  # falas traduzidas agora (as que faltavam)
    untranslated: int = 0  # falas que ficaram no original
    problems: list[str] = field(default_factory=list)  # páginas (ou o capítulo inteiro) que não abriram


def generate_chapter(
    db: Database,
    pipeline: Pipeline,
    config: Config,
    chapter_id: int,
    folder: Path,
    fmt: str,
    translate_missing: bool,
    stop: threading.Event,
    on_page: Callable[[str, int], None] = lambda _chapter, _page: None,
    status: Callable[[str], None] = lambda _msg: None,
    inpaint: bool = False,
) -> ChapterResult:
    """Gera um capítulo. `inpaint`: reconstrói o desenho por baixo do texto fora dos balões (ver inpaint.py). `config` já aplicada à obra (ver work_config). Erros do tradutor sobem (TranslationError,
    PipelineError); o que já foi traduzido fica salvo no banco, e o arquivo do capítulo não é gravado."""
    work_id, chapter_name, order = db.chapter_info(chapter_id)
    work = db.work(work_id)
    pages = db.stored_pages(chapter_id)
    key = Pipeline._db_key(config)
    result = ChapterResult(output_path(folder, work.name, chapter_name, fmt))
    if not pages or not Path(pages[0].origin).exists():
        origin = pages[0].origin if pages else "sem páginas"
        result.problems.append(f"{chapter_name}: o arquivo original não foi encontrado ({origin}); foi movido ou apagado?")
        result.path = None
        return result
    output = _Output(result.path, fmt)
    digits = max(3, len(str(len(pages))))
    try:
        for index, page in enumerate(pages, start=1):
            if stop.is_set():
                output.discard()
                result.path = None
                return result
            on_page(chapter_name, page.number)
            try:
                image = load_page(page.origin, page.file)
            except Exception as exc:  # arquivo original movido ou corrompido: o capítulo sai sem a página
                result.problems.append(f"{chapter_name}, página {page.number}: {exc or exc.__class__.__name__}")
                continue
            if page.regions:
                image = _translated_page(db, pipeline, config, key, image, page, translate_missing, result, status, inpaint)
            output.add(f"{index:0{digits}d}.jpg", encode_page(image))
            result.pages += 1
        if not result.pages:  # nenhuma página abriu: um capítulo vazio não serve para nada
            output.discard()
            result.path = None
            return result
        if fmt == "cbz":
            output.add("ComicInfo.xml", comic_info(work.name, chapter_name, order, config.target_lang, result.pages).encode())
        output.commit()
    except BaseException:
        output.discard()
        raise
    return result


def _translated_page(
    db: Database,
    pipeline: Pipeline,
    config: Config,
    key: TranslationKey,
    image: Image.Image,
    page: StoredPage,
    translate_missing: bool,
    result: ChapterResult,
    status: Callable[[str], None],
    inpaint: bool = False,
) -> Image.Image:
    boxes = [box for box, _t in page.regions]
    texts = [text for _b, text in page.regions]
    translations = saved_translations(db, key, texts)
    pending = [(i, t) for i, t in enumerate(texts) if normalize(t) and normalize(t) not in translations]
    if pending and translate_missing:
        new = pipeline.translate_stored(image, [Region(b, b, None) for b in boxes], pending, config, status)
        translations.update({normalize(texts[i]): translation for i, translation in new.items()})
        result.translated += len(new)
    result.untranslated += sum(1 for _i, t in pending if normalize(t) not in translations)
    if not any(normalize(t) in translations for t in texts):
        return image  # nada para desenhar: nem precisa do detector
    if page.layouts and all(page.layouts):
        # Balão e área guardados na importação: não precisa do detector
        regions = [Region(box, area, bubble) for box, (area, bubble) in zip(boxes, page.layouts)]
    else:
        regions = pipeline.page_layout(image, boxes, config)
    erased: frozenset[int] = frozenset()
    if inpaint:
        # Só o texto sobre o desenho: no papel liso de um balão, cobrir já resolve. O critério é o que está em volta do
        # texto, e não "ter balão": com o limiar baixo dos arquivos, o detector vê balões em volta de onomatopeias
        frame = np.asarray(image.convert("RGB"))
        drawable = {
            i for i, (region, text) in enumerate(zip(regions, texts))
            if (tr := translations.get(normalize(text))) and tr.casefold() != text.casefold()
            and over_art(frame, region.text_box, _expand(region.text_box, image.size))
        }
        if drawable:
            boxes_to_erase: list = []
            for i in sorted(drawable):
                if not any(same_place(regions[i].text_box, other) for other in boxes_to_erase):
                    boxes_to_erase.append(regions[i].text_box)
            image = pipeline.erase_text(image, boxes_to_erase, config)
            erased = frozenset(drawable)
    image, drawn = render_page(image, regions, texts, translations, font_of(config), config.target_lang, erased)
    result.drawn += drawn
    return image


_fonts: dict[str, object] = {}


def font_of(config: Config):
    """A fonte do overlay (Configurações), criada uma vez por família."""
    from .render import base_font

    if config.font_family not in _fonts:
        _fonts[config.font_family] = base_font(config.font_family)
    return _fonts[config.font_family]


# --- execução em segundo plano -------------------------------------------------------------


@dataclass
class GenerateProgress:
    done: int  # páginas
    total: int
    chapter: str
    page: int
    seconds_left: float | None


@dataclass
class GenerateSummary:
    folder: Path
    files: list[Path]
    pages: int
    drawn: int
    translated: int
    untranslated: int
    problems: list[str]
    cancelled: bool
    error: str | None  # o tradutor falhou (chave, rede, crédito): parou no capítulo em que estava


class Generator(QObject):
    progress = Signal(object)  # GenerateProgress
    finished = Signal(object)  # GenerateSummary

    def __init__(self, db: Database, pipeline: Pipeline, parent: QObject | None = None):
        super().__init__(parent)
        self._db = db
        self._pipeline = pipeline
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self, config: Config, work: Work, chapter_ids: list[int], folder: Path, fmt: str, translate_missing: bool, inpaint: bool = False
    ) -> bool:
        if self.running:
            return False
        self._stop.clear()
        args = (work_config(config, work), work, chapter_ids, folder, fmt, translate_missing, inpaint)
        self._thread = threading.Thread(target=self._run, args=args, name="gerar-capitulos", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Para antes da próxima página; o capítulo em andamento não é gravado (os já prontos ficam)."""
        self._stop.set()

    def wait(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(
        self, config: Config, work: Work, chapter_ids: list[int], folder: Path, fmt: str, translate_missing: bool, inpaint: bool
    ) -> None:
        pages = {c.id: c.pages for c in self._db.chapters(work.id)}
        total = sum(pages.get(c, 0) for c in chapter_ids)
        summary = GenerateSummary(folder, [], 0, 0, 0, 0, [], cancelled=False, error=None)
        started = time.perf_counter()
        done = 0

        def on_page(chapter: str, page: int) -> None:
            nonlocal done
            elapsed = time.perf_counter() - started
            seconds_left = elapsed / done * (total - done) if done >= 3 else None
            self.progress.emit(GenerateProgress(done, total, chapter, page, seconds_left))
            done += 1

        for chapter_id in chapter_ids:
            try:
                result = generate_chapter(
                    self._db, self._pipeline, config, chapter_id, folder, fmt, translate_missing, self._stop, on_page,
                    inpaint=inpaint,
                )
            except (TranslationError, PipelineError) as exc:
                summary.error = str(exc)
                break
            except OSError as exc:  # pasta de destino sem permissão, disco cheio
                summary.error = f"Não foi possível gravar em {folder}: {exc}"
                break
            except Exception as exc:  # inesperado (ex.: falta de memória na GPU): vira mensagem, não some com a thread
                import traceback

                traceback.print_exc()
                summary.error = f"erro inesperado: {exc.__class__.__name__}: {exc}"
                break
            summary.pages += result.pages
            summary.drawn += result.drawn
            summary.translated += result.translated
            summary.untranslated += result.untranslated
            summary.problems += result.problems
            if result.path is None:
                if self._stop.is_set():
                    summary.cancelled = True
                    break
                continue  # o original do capítulo sumiu (está em `problems`): segue para o próximo
            summary.files.append(result.path)
        self.finished.emit(summary)


# --- linha de comando -----------------------------------------------------------------------


def run_cli(
    folder: str, works: list[str] | None, chapters: list[str] | None, fmt: str, translate_missing: bool, inpaint: bool = False
) -> int:
    """`--gerar PASTA --obra NOME [--capitulo NOME…] [--formato cbz|pasta] [--traduzir-faltantes] [--reconstruir]`."""
    offscreen_qt()
    from PySide6.QtGui import QGuiApplication

    if fmt not in FORMATS:
        print(f"Formato desconhecido: {fmt}. Opções: {', '.join(FORMATS)}", file=sys.stderr)
        return 2
    if not works:
        print("Diga de qual obra com --obra NOME (pode repetir).", file=sys.stderr)
        return 2
    _app = QGuiApplication(sys.argv[:1])  # a fonte do desenho precisa de uma aplicação Qt
    db = Database()
    pipeline = Pipeline(db)
    config = Config.load()
    wanted = {c.casefold() for c in chapters or []}
    exit_code = 0
    try:
        for name in works:
            work = next((w for w in db.works() if w.name.casefold() == name.casefold()), None)
            if work is None:
                print(f"Obra não encontrada: {name}", file=sys.stderr)
                exit_code = 1
                continue
            chosen = [c for c in db.chapters(work.id) if not wanted or c.name.casefold() in wanted]
            if not chosen:
                print(f"{work.name}: nenhum capítulo importado{' com esse nome' if wanted else ''}.", file=sys.stderr)
                exit_code = 1
                continue
            for chapter in chosen:
                result = generate_chapter(
                    db, pipeline, work_config(config, work), chapter.id, Path(folder), fmt, translate_missing,
                    threading.Event(), status=lambda message: print(message, file=sys.stderr), inpaint=inpaint,
                )
                extra = f", {result.translated} traduzida(s) agora" if result.translated else ""
                left = f", {result.untranslated} sem tradução" if result.untranslated else ""
                if result.path is not None:
                    print(f"{result.path}: {result.pages} página(s), {result.drawn} fala(s) desenhada(s){extra}{left}")
                else:
                    exit_code = 1
                for problem in result.problems:
                    print(f"  aviso: {problem}", file=sys.stderr)
    except (TranslationError, PipelineError) as exc:
        print(f"Erro ao traduzir: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Erro ao gravar: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()
    return exit_code
