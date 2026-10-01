"""Fluxo completo com importador, lote e geração falsos (python -m pytest tests)."""

from pathlib import Path

import pytest

from mangaoverlay import flow as flow_module
from mangaoverlay.config import Config
from mangaoverlay.db import Database, TranslationKey
from mangaoverlay.flow import FlowOptions, FlowRunner, read_log
from mangaoverlay.generate import GenerateSummary


class FakeImporter:
    def __init__(self, db):
        self.db, self.running, self.starts = db, False, 0

    def start(self, config):
        self.starts += 1
        for page in self.db.pending_pages():  # "lê" tudo na hora
            self.db.save_page_texts(page.id, [((0, 0, 10, 10), f"fala {page.id}")])
        return True


class FakeBatches:
    def __init__(self):
        self.running, self.starts = False, 0

    def start(self, config):
        self.starts += 1
        return True


class FakeGenerator:
    def __init__(self):
        self.running, self.calls = False, []

    def start(self, config, work, chapter_ids, folder, fmt, translate_missing, inpaint):
        self.calls.append((chapter_ids, folder, fmt, inpaint))
        return True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(flow_module, "LOG_FILE", tmp_path / "fluxo.log")
    db = Database(tmp_path / "banco.db")
    work = db.create_work("Obra", "ja")
    chapter = db.add_chapter(work.id, "Cap 1", 1, "/m/cap1", ["1.png", "2.png"])
    importer, batches, generator = FakeImporter(db), FakeBatches(), FakeGenerator()
    notes = []
    runner = FlowRunner(db, lambda: Config(), importer, batches, generator, lambda m, w: notes.append(m))
    yield db, work, chapter, runner, importer, batches, generator, notes
    db.close()


def _options(chapter, **extra):
    return FlowOptions([chapter], engine="local", openai_model="gpt-4.1-mini", claude_model="claude-opus-5", **extra)


def test_fluxo_inteiro(setup, tmp_path):
    db, work, chapter, runner, importer, batches, generator, notes = setup
    flow_id = runner.start(work.id, _options(chapter, generate=True, folder=str(tmp_path / "saida"), fmt="pasta", inpaint=True))
    assert db.flow(flow_id).step == "importar" and "Lendo 2" in db.flow(flow_id).message
    runner.tick()  # a importação terminou (no app, o sinal de fim chama o tick)
    flow = db.flow(flow_id)
    # Leu, custo grátis (NLLB) e criou o lote
    assert importer.starts == 1 and flow.step == "traduzir" and flow.batch_id is not None and batches.starts == 1
    # O lote terminou: vai para a geração, com as escolhas do assistente
    db.set_batch_state(flow.batch_id, "concluido")
    runner.tick()
    assert db.flow(flow_id).step == "gerar"
    assert generator.calls == [([chapter], Path(tmp_path / "saida"), "pasta", True)]
    attention = []
    runner.attention.connect(attention.append)
    runner.generation_finished(GenerateSummary(tmp_path / "saida", [tmp_path / "saida" / "Cap 1"], 2, 5, 0, 0, [], False, None))
    flow = db.flow(flow_id)
    assert (flow.state, flow.step) == ("concluido", "fim") and "1 arquivo" in flow.message and notes
    assert attention == [flow_id]  # o app abre a janela do fluxo no fim
    # O passo a passo ficou no log (arquivo), na ordem
    log = [e.message for e in read_log(flow_id, flow_module.LOG_FILE)]
    assert log[0].startswith("Fluxo iniciado") and log[-1].startswith("Fluxo concluído")
    assert any(m.startswith("Lendo 2 página") for m in log) and any(m.startswith("Gerado: 1 arquivo") for m in log)


def test_custo_acima_do_limite_pausa_e_continua_se_aceito(setup):
    db, work, chapter, runner, importer, batches, generator, notes = setup
    options = _options(chapter, cost_limit=0.0)
    options.engine = "openai-text"
    flow_id = runner.start(work.id, options)
    runner.tick()
    flow = db.flow(flow_id)
    assert (flow.state, flow.step) == ("pausado", "custo") and "acima do limite" in flow.message
    assert batches.starts == 0  # nada foi enviado
    runner.resume(flow_id, accept_cost=True)
    flow = db.flow(flow_id)
    assert (flow.state, flow.step) == ("ativo", "traduzir") and flow.batch_id is not None


def test_nada_a_traduzir_pula_para_o_fim(setup):
    db, work, chapter, runner, importer, batches, generator, notes = setup
    importer.start(None)
    key = TranslationKey(work.id, "ja", "pt", "local", "facebook/nllb-200-distilled-600M")
    db.save_translations(key, [(t, "ok") for _p, t in db.chapter_lines([chapter])[chapter]])
    flow_id = runner.start(work.id, _options(chapter))
    assert db.flow(flow_id).state == "concluido" and batches.starts == 0


def test_lote_pausado_espera_e_continua(setup):
    db, work, chapter, runner, importer, batches, generator, notes = setup
    flow_id = runner.start(work.id, _options(chapter))
    runner.tick()
    batch_id = db.flow(flow_id).batch_id
    db.set_batch_state(batch_id, "pausado")
    runner.tick()
    flow = db.flow(flow_id)
    assert flow.state == "ativo" and "pausada" in flow.message  # espera, não desiste
    db.set_batch_state(batch_id, "concluido")
    runner.tick()
    assert db.flow(flow_id).state == "concluido"  # sem gerar resultado: termina
