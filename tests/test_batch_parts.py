"""Lote com capítulos de memória e envio em partes, com a OpenAI simulada (python -m pytest tests)."""

import pytest

from mangaoverlay import batch as batch_module
from mangaoverlay.batch import BatchOptions, BatchRunner, create_batch, plan_groups, split_parts
from mangaoverlay.config import Config
from mangaoverlay.db import Database, TranslationKey
from mangaoverlay.names import SurveyOutcome
from mangaoverlay.translators import Usage

CONFIG = Config(engine="openai-text", openai_model="gpt-4.1-mini", target_lang="pt", memory_summary=False)


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_module, "LOG_FILE", tmp_path / "lote.log")
    db = Database(tmp_path / "banco.db")
    work = db.create_work("Obra", "ja")
    chapters = []
    for number, pages in enumerate([2, 3, 1, 4], start=1):
        chapter = db.add_chapter(work.id, f"Cap {number}", number, f"/m/cap{number}", [f"{p}.png" for p in range(pages)])
        for page in db.pending_pages(work.id):
            db.save_page_texts(page.id, [((0, 0, 10, 10), f"fala {number}-{page.number}")])
        chapters.append(chapter)
    yield db, work, chapters
    db.close()


def test_partes_respeitam_capitulos_e_equilibram_paginas():
    assert split_parts([2, 3, 1, 4], 1) == [[0, 1, 2, 3]]
    assert split_parts([2, 3, 1, 4], 2) == [[0, 1], [2, 3]]
    assert split_parts([5, 5, 5], 3) == [[0], [1], [2]]
    # Mais partes que capítulos: uma parte por capítulo, nunca parte vazia
    assert split_parts([1, 1], 5) == [[0], [1]]


def test_grupos_memoria_e_partes(work):
    db, _work, chapters = work
    options = BatchOptions(chapters, pages_per_block=2, mode="batch", memory_chapters=1, memory_block=1, parts=2)
    groups = plan_groups(db, options)
    assert [(len(g.pages), g.block, g.sync, g.part) for g in groups] == [(2, 1, True, 0), (4, 2, False, 1), (4, 2, False, 2)]
    # Sem escolher o tamanho dos pedidos de memória, vale o do lote
    options.memory_block = 0
    assert plan_groups(db, options)[0].block == 2
    # Envio normal: tudo junto, sem memória nem partes
    options.mode = "normal"
    assert [(len(g.pages), g.sync, g.part) for g in plan_groups(db, options)] == [(10, False, 0)]


def test_envia_uma_parte_por_vez(work, monkeypatch):
    db, work_, chapters = work
    options = BatchOptions(chapters, pages_per_block=10, mode="batch", memory_chapters=1, parts=2)
    batch_id = create_batch(db, CONFIG, work_.id, "ja", options, None)
    key = TranslationKey(work_.id, "ja", "pt", "openai-text", "gpt-4.1-mini")
    sent: list[set[int]] = []

    def submit(api_key, payload):
        sent.append({int(k) for k in payload})
        return f"remoto-{len(sent)}"

    monkeypatch.setattr(batch_module.openai_batch, "submit", submit)
    runner = BatchRunner(db, pipeline=None)
    requests = db.batch_requests(batch_id, ("pendente",))
    memory = [r for r in requests if r.sync]
    part1 = [r.id for r in requests if r.part == 1]
    part2 = [r.id for r in requests if r.part == 2]
    assert memory and part1 and part2

    # Os capítulos de memória já foram traduzidos na hora
    for r in memory:
        db.record_request(r.id, "concluida", (0, 0, 0), 0.0)
        db.save_translations(key, [(line.text, "ok") for line in db.request_lines(r.id)])

    batch = db.batches()[0]
    runner._submit_remote(batch, key, "chave", 1, CONFIG)
    assert sent == [set(part1)]  # só a parte 1

    # A OpenAI devolveu a parte 1 inteira: o próximo envio já é a parte 2
    for request_id in part1:
        db.save_translations(key, [(line.text, "ok") for line in db.request_lines(request_id)])
    runner._submit_remote(db.batches()[0], key, "chave", 2, CONFIG)
    assert sent[-1] == set(part2)
    assert db.batches()[0].round == 1  # a contagem de rodadas recomeça na parte nova
    assert {r.id for r in db.batch_requests(batch_id, ("concluida",))} >= set(part1)


def test_nomes_levantados_antes_de_cada_parte(work, monkeypatch):
    db, work_, chapters = work
    options = BatchOptions(chapters, pages_per_block=10, mode="batch", parts=2, survey_names=True)
    create_batch(db, CONFIG, work_.id, "ja", options, None)
    surveyed: list[list[int]] = []

    def survey(db_, work_id, config, chapter_ids, source, target):
        surveyed.append(chapter_ids)
        return SurveyOutcome([], 0, 0.0, Usage())

    monkeypatch.setattr(batch_module, "survey_and_save", survey)
    monkeypatch.setattr(batch_module.openai_batch, "submit", lambda api_key, payload: "remoto")
    key = TranslationKey(work_.id, "ja", "pt", "openai-text", "gpt-4.1-mini")
    BatchRunner(db, pipeline=None)._submit_remote(db.batches()[0], key, "chave", 1, CONFIG)
    assert surveyed == [chapters[:2]]  # só os capítulos da parte 1
