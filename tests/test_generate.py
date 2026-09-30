"""Capítulos traduzidos em imagem, sem os modelos: detector e tradutor simulados (python -m pytest tests)."""

import io
import os
import shutil
import threading
import zipfile

import numpy as np
import pytest
from PIL import Image, ImageDraw

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QGuiApplication  # noqa: E402

from mangaoverlay.config import Config  # noqa: E402
from mangaoverlay.db import Database, TranslationKey  # noqa: E402
from mangaoverlay.detector import Region  # noqa: E402
from mangaoverlay.generate import (  # noqa: E402
    estimate_missing,
    generate_chapter,
    missing_by_chapter,
    output_path,
    work_config,
)
from mangaoverlay.pipeline import Pipeline  # noqa: E402

TEXT = (60, 60, 140, 140)  # "texto" (bloco preto) da página 1
OTHER = (60, 200, 140, 280)  # fala da página 2, sem tradução salva
CONFIG = Config(engine="local", target_lang="pt", source_lang="ja")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QGuiApplication.instance() or QGuiApplication([])


class FakePipeline:
    """Detector que devolve um balão em volta de cada fala e tradutor que só anota o que recebeu."""

    def __init__(self, db: Database):
        self.db = db
        self.translated: list[str] = []

    def page_layout(self, image, boxes, config):
        return [Region(b, b, (b[0] - 30, b[1] - 30, b[2] + 30, b[3] + 30)) for b in boxes]

    def translate_stored(self, image, regions, pending, config, status=None):
        self.translated += [t for _i, t in pending]
        self.db.save_translations(Pipeline._db_key(config), [(t, f"[{t}]") for _i, t in pending])
        return {i: f"[{t}]" for i, t in pending}


def _page(box) -> Image.Image:
    image = Image.new("RGB", (200, 340), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((5, 5, 195, 335), outline=(0, 0, 0), width=3)  # quadro
    draw.rectangle(box, fill=(10, 10, 10))
    return image


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / "cap1"
    source.mkdir()
    _page(TEXT).save(source / "01.png")
    _page(OTHER).save(source / "02.png")
    db = Database(tmp_path / "banco.db")
    work = db.create_work("Obra: Teste", "ja")
    chapter = db.add_chapter(work.id, "Cap 1", 1, str(source), ["01.png", "02.png"])
    first, second = db.pending_pages(work.id)
    db.save_page_texts(first.id, [(TEXT, "お前は誰だ")])
    db.save_page_texts(second.id, [(OTHER, "ここはどこ")])
    # Traduzida com outro motor (lote com o GPT): vale também para quem gera com o tradutor offline
    db.save_translations(TranslationKey(work.id, "ja", "pt", "openai-text", "gpt-4.1-mini"), [("お前は誰だ", "Quem é você?")])
    yield db, work, chapter, tmp_path / "saida"
    db.close()


def _generate(setup, fmt="cbz", translate=False, stop=None):
    db, work, chapter, out = setup
    pipeline = FakePipeline(db)
    result = generate_chapter(db, pipeline, work_config(CONFIG, work), chapter, out, fmt, translate, stop or threading.Event())
    return result, pipeline


def _darkness(image: Image.Image, box) -> float:
    return 255 - float(np.asarray(image.convert("L").crop(box)).mean())


def test_gera_cbz_com_a_traducao_salva(setup):
    result, pipeline = _generate(setup)
    assert result.path.name == "Obra_ Teste - Cap 1 (traduzido).cbz"
    assert (result.pages, result.drawn, result.untranslated, result.translated) == (2, 1, 1, 0)
    assert pipeline.translated == []  # sem a opção, nada vai ao tradutor
    with zipfile.ZipFile(result.path) as archive:
        assert archive.namelist() == ["001.jpg", "002.jpg", "ComicInfo.xml"]
        first = Image.open(io.BytesIO(archive.read("001.jpg")))
        second = Image.open(io.BytesIO(archive.read("002.jpg")))
        info = archive.read("ComicInfo.xml").decode()
    assert first.mode == "L"  # página em preto e branco: gravada em tons de cinza
    # O bloco preto da página 1 foi coberto pela tradução; a página 2 (sem tradução) ficou como estava
    assert _darkness(first, TEXT) < 120
    assert _darkness(second, OTHER) > 230
    assert "<Series>Obra: Teste</Series>" in info and "<Number>1</Number>" in info and "<PageCount>2</PageCount>" in info
    assert not list(result.path.parent.glob("*.parcial"))


def test_traduz_as_que_faltam_quando_pedido(setup):
    db, work, _chapter, _out = setup
    result, pipeline = _generate(setup, translate=True)
    assert pipeline.translated == ["ここはどこ"]  # só a que faltava
    assert (result.drawn, result.translated, result.untranslated) == (2, 1, 0)
    # Gravada no banco: gerar de novo não traduz outra vez
    again, pipeline = _generate(setup, translate=True)
    assert pipeline.translated == [] and again.drawn == 2


def test_pasta_de_imagens_substitui_a_anterior(setup):
    first, _ = _generate(setup, fmt="pasta")
    (first.path / "sobra.jpg").write_bytes(b"antiga")
    second, _ = _generate(setup, fmt="pasta")
    assert second.path == first.path and second.path.is_dir()
    assert sorted(p.name for p in second.path.iterdir()) == ["001.jpg", "002.jpg"]


def test_parar_nao_grava_capitulo_pela_metade(setup):
    stop = threading.Event()
    stop.set()
    result, _ = _generate(setup, stop=stop)
    _db, _work, _chapter, out = setup
    assert result.path is None
    assert not out.exists() or not list(out.iterdir())


def test_pagina_que_nao_abre_fica_de_fora(setup):
    db, work, chapter, out = setup
    (out.parent / "cap1" / "02.png").unlink()
    result, _ = _generate(setup)
    assert result.pages == 1 and len(result.problems) == 1 and "página 2" in result.problems[0]


def test_estimativa_das_falas_que_faltam(setup):
    db, work, chapter, _out = setup
    missing = missing_by_chapter(db, CONFIG, work)
    assert [t for _p, t in missing[chapter]] == ["ここはどこ"]
    estimate = estimate_missing(db, CONFIG, work, missing, [chapter])
    assert (estimate.lines, estimate.pages, estimate.cost, estimate.unread_pages) == (1, 1, 0.0, 0)
    paid = estimate_missing(db, Config(engine="openai-text", openai_model="gpt-4.1-mini"), work, missing, [chapter])
    assert paid.cost is not None and 0 < paid.cost < 0.01


def test_nome_do_arquivo(tmp_path):
    assert output_path(tmp_path, "Obra", "Obra - Cap 3", "cbz").name == "Obra - Cap 3 (traduzido).cbz"
    assert output_path(tmp_path, "A/B", 'Cap "1"?', "pasta").name == "A_B - Cap _1__ (traduzido)"


def test_original_sumiu_nao_grava_nada(setup):
    db, work, chapter, out = setup
    shutil.rmtree(out.parent / "cap1")
    result, _ = _generate(setup)
    assert result.path is None and len(result.problems) == 1 and "não foi encontrado" in result.problems[0]
    assert not out.exists() or not list(out.iterdir())


def test_traducao_feita_com_outro_idioma_de_origem(setup):
    """A obra era "en" quando foi traduzida e depois passou a "ja": as traduções continuam valendo."""
    db, work, _chapter, _out = setup
    db.save_translations(TranslationKey(work.id, "en", "pt", "openai-text", "gpt-4.1-mini"), [("ここはどこ", "Onde estamos?")])
    result, _ = _generate(setup)
    assert (result.drawn, result.untranslated) == (2, 0)
