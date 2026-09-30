"""Modo em tempo real: quando esconder e quando traduzir de novo (python -m pytest tests)."""

import numpy as np

from mangaoverlay.realtime import ChangeDetector


def _page(seed: int) -> dict[str, np.ndarray]:
    """Uma "página" na miniatura de um monitor (conteúdo diferente para cada semente)."""
    return {"tela": np.random.default_rng(seed).integers(0, 256, (160, 256), dtype=np.uint8)}


def _with_clock(frame: dict[str, np.ndarray], minute: int) -> dict[str, np.ndarray]:
    """Mesma tela com o relógio da barra de tarefas mudando (poucos pixels)."""
    copy = {k: v.copy() for k, v in frame.items()}
    copy["tela"][150:156, 240:252] = (minute * 37) % 256
    return copy


def _scrolled(frame: dict[str, np.ndarray], pixels: int) -> dict[str, np.ndarray]:
    return {k: np.roll(v, -pixels, axis=0) for k, v in frame.items()}


def _run(detector: ChangeDetector, frames) -> list[str]:
    return [detector.feed(f) for f in frames]


def test_tela_parada_nao_faz_nada():
    detector = ChangeDetector()
    page = _page(1)
    assert _run(detector, [page] * 10) == ["nada"] * 10


def test_relogio_e_cursor_nao_contam_como_mudanca():
    detector = ChangeDetector()
    page = _page(1)
    assert set(_run(detector, [page] + [_with_clock(page, m) for m in range(8)])) == {"nada"}


def test_trocar_de_pagina_esconde_e_traduz_quando_parar():
    detector = ChangeDetector()
    a, b = _page(1), _page(2)
    events = _run(detector, [a, a, b, b, b, b])
    assert events == ["nada", "nada", "mudou", "nada", "parou", "nada"]
    assert detector.state == "ocupado"


def test_rolagem_so_traduz_quando_termina():
    detector = ChangeDetector()
    page = _page(3)
    rolling = [_scrolled(page, 8 * i) for i in range(1, 7)]  # 6 quadros rolando
    final = rolling[-1]
    events = _run(detector, [page, *rolling, final, final])
    assert events[1] == "mudou"
    assert "parou" not in events[:7]  # enquanto rola, nunca traduz
    assert events[-1] == "parou"


def test_ocupado_ignora_ate_rearmar():
    detector = ChangeDetector()
    a, b, c = _page(1), _page(2), _page(3)
    _run(detector, [a, b, b, b])  # parou: o app vai traduzir
    assert _run(detector, [c, a, b]) == ["nada"] * 3  # traduzindo: nada muda o estado
    detector.rearm()
    # A tela com a tradução vira a referência; só uma nova mudança dispara de novo
    assert _run(detector, [c, c, a]) == ["nada", "nada", "mudou"]


def test_monitor_ligado_ou_desligado_conta_como_mudanca():
    detector = ChangeDetector()
    a = _page(1)
    two = {**a, "outra": _page(5)["tela"]}
    assert _run(detector, [a, two]) == ["nada", "mudou"]


def test_tela_mudou_enquanto_traduzia_descarta_o_resultado():
    """A página nova abriu devagar (janela do visualizador aparecendo) no meio da tradução da tela antiga."""
    detector = ChangeDetector()
    a, b, c = _page(1), _page(2), _page(3)
    assert _run(detector, [a, b, b, b])[-1] == "parou"  # traduzindo a tela b
    _run(detector, [b, c, c])  # durante a tradução, a tela virou c
    assert detector.freeze() is True  # o resultado é da tela b: descartar
    detector.changed_again()
    assert _run(detector, [c, c, c])[-1] == "parou"  # c parou: traduz de novo


def test_tela_parada_durante_a_traducao_mostra_o_resultado():
    detector = ChangeDetector()
    a, b = _page(1), _page(2)
    _run(detector, [a, b, b, b])
    _run(detector, [b, b, _with_clock(b, 3)])
    assert detector.freeze() is False


def test_pedido_manual_compara_com_a_tela_do_comeco():
    detector = ChangeDetector()
    a, b = _page(1), _page(2)
    _run(detector, [a, a])
    detector.busy()  # atalho apertado: sem quadro "parado" de referência, vale o segundo durante a tradução
    _run(detector, [b, a, b])  # o primeiro quadro (ainda com a tradução anterior) é ignorado
    assert detector.freeze() is True


def test_pedido_manual_ignora_a_traducao_anterior_sumindo():
    detector = ChangeDetector()
    a, with_old_overlay = _page(1), _page(4)
    detector.busy()
    _run(detector, [with_old_overlay, a, a])
    assert detector.freeze() is False
