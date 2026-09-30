"""Exportar e importar dados (python -m pytest tests)."""

import json
import zipfile

import pytest

from mangaoverlay import transfer
from mangaoverlay.db import Character, Database, Term, TranslationKey


def _populated(path) -> tuple[Database, int]:
    db = Database(path)
    work = db.create_work("Obra A", "ja")
    db.add_chapter(work.id, "Cap 1", 1, "/mangas/obra-a/cap1", ["01.png", "02.png"])
    for page in db.pending_pages(work.id):  # "lidas" como o importador faria
        db.save_page_texts(page.id, [((10, 20, 110, 220), "お前は誰だ"), ((200, 20, 300, 220), "ここはどこ")])
    db.add_chapter(work.id, "Cap 2", 2, "/mangas/obra-a/cap2", ["01.png"])  # ainda não lido (pendente)
    key = TranslationKey(work.id, "ja", "pt", "openai-text", "gpt-4.1-mini")
    db.save_translations(key, [("お前は誰だ", "Quem é você?"), ("ここはどこ", "Onde estamos?")])
    db.save_translations(TranslationKey(None, "ja", "pt", "local", "nllb"), [("はい", "Sim")])
    db.save_characters(work.id, [Character("Haruto", "春斗", "masculino"), Character("Mina")])
    db.set_summary(work.id, "Haruto acorda num lugar estranho.")
    db.add_terms(work.id, [Term("魔王", "Rei Demônio")])
    db.consolidate_terms(work.id, 80)
    other = db.create_work("Obra B", "ko")
    db.save_translations(TranslationKey(other.id, "ko", "pt", "local", "nllb"), [("안녕", "Oi")])
    return db, work.id


def _rows(db: Database, sql: str) -> list:
    with db.locked() as conn:
        return conn.execute(sql).fetchall()


def test_ida_e_volta(tmp_path):
    source, work_id = _populated(tmp_path / "origem.db")
    counts = transfer.export_data(source, tmp_path / "exportado.zip", [work_id], loose=True)
    assert counts == {"obras": 1, "capitulos": 2, "paginas": 3, "traducoes": 3}

    target = Database(tmp_path / "destino.db")
    data = transfer.read_data(tmp_path / "exportado.zip")
    preview = transfer.preview(target, data)
    assert [(w.name, w.exists, w.new_chapters) for w in preview.works] == [("Obra A", False, 2)]
    result = transfer.import_data(target, data)
    assert (result.works_created, result.chapters, result.pages, result.translations) == (1, 2, 3, 3)

    work = target.works()[0]
    assert work.name == "Obra A" and work.source_lang == "ja"
    assert sorted(t for (t,) in _rows(target, "SELECT traducao FROM traducoes")) == ["Onde estamos?", "Quem é você?", "Sim"]
    assert _rows(target, "SELECT COUNT(*) FROM traducoes WHERE obra_id IS NULL") == [(1,)]
    # Texto e posição de cada balão, na ordem de leitura
    regions = target.work_regions(work.id)
    assert len(regions) == 4 and regions[0][1:] == ((10, 20, 110, 220), "お前は誰だ")
    assert [c.name for c in target.characters(work.id)] == ["Haruto", "Mina"]
    assert target.characters(work.id)[0].original == "春斗"
    memory = target.memory(work.id)
    assert memory.summary == "Haruto acorda num lugar estranho." and memory.glossary[0].translation == "Rei Demônio"
    # A página não lida continua pendente (será lida se o arquivo existir nesta máquina)
    assert _rows(target, "SELECT estado, COUNT(*) FROM paginas GROUP BY estado ORDER BY estado") == [("lida", 2), ("pendente", 1)]


def test_importar_duas_vezes_nao_duplica(tmp_path):
    source, work_id = _populated(tmp_path / "origem.db")
    transfer.export_data(source, tmp_path / "e.zip", [work_id], loose=True)
    target = Database(tmp_path / "destino.db")
    data = transfer.read_data(tmp_path / "e.zip")
    transfer.import_data(target, data)
    again = transfer.import_data(target, data)
    assert (again.works_created, again.works_merged, again.chapters, again.chapters_skipped) == (0, 1, 0, 2)
    assert (again.translations, again.translations_skipped, again.characters, again.terms) == (0, 3, 0, 0)
    assert _rows(target, "SELECT COUNT(*) FROM paginas") == [(3,)]


def test_mescla_sem_sobrescrever(tmp_path):
    source, work_id = _populated(tmp_path / "origem.db")
    transfer.export_data(source, tmp_path / "e.zip", [work_id])
    target = Database(tmp_path / "destino.db")
    mine = target.create_work("obra a", "ja")  # mesmo nome, outra caixa
    target.add_chapter(mine.id, "Cap 1 (meu)", 1, "/mangas/obra-a/cap1", ["01.png"])
    target.save_translations(TranslationKey(mine.id, "ja", "pt", "openai-text", "gpt-4.1-mini"), [("お前は誰だ", "Quem é tu?")])
    target.set_summary(mine.id, "Meu resumo.")

    result = transfer.import_data(target, transfer.read_data(tmp_path / "e.zip"))
    assert (result.works_created, result.works_merged, result.chapters, result.chapters_skipped) == (0, 1, 1, 1)
    assert len(target.works()) == 1
    # O que já existia vence
    assert _rows(target, "SELECT traducao FROM traducoes WHERE texto_original = 'お前は誰だ'") == [("Quem é tu?",)]
    assert target.memory(mine.id).summary == "Meu resumo."
    assert sorted(c.name for c in target.chapters(mine.id)) == ["Cap 1 (meu)", "Cap 2"]


def test_so_as_obras_escolhidas(tmp_path):
    source, work_id = _populated(tmp_path / "origem.db")
    transfer.export_data(source, tmp_path / "e.zip", [work_id])
    data = transfer.read_data(tmp_path / "e.zip")
    assert [w["nome"] for w in data["obras"]] == ["Obra A"] and data["traducoes_sem_obra"] == []


def test_arquivo_invalido(tmp_path):
    (tmp_path / "x.zip").write_bytes(b"nada")
    with pytest.raises(transfer.TransferError):
        transfer.read_data(tmp_path / "x.zip")
    with zipfile.ZipFile(tmp_path / "novo.zip", "w") as archive:
        archive.writestr("dados.json", json.dumps({"formato": "mangaoverlay", "versao": transfer.VERSION + 1}))
    with pytest.raises(transfer.TransferError, match="mais nova"):
        transfer.read_data(tmp_path / "novo.zip")


def test_erro_no_meio_desfaz_tudo(tmp_path):
    source, work_id = _populated(tmp_path / "origem.db")
    transfer.export_data(source, tmp_path / "e.zip", [work_id])
    data = transfer.read_data(tmp_path / "e.zip")
    data["obras"][0]["capitulos"][1]["paginas"][0]["estado"] = "estado-invalido"  # viola o CHECK do banco
    target = Database(tmp_path / "destino.db")
    with pytest.raises(Exception):
        transfer.import_data(target, data)
    assert target.works() == []
