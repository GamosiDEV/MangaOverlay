"""Modo em tempo real: quando esconder e quando traduzir de novo (python -m pytest tests)."""

import numpy as np

from mangaoverlay.realtime import ChangeDetector, only_available


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
    # A tela vira a referência depois de ~1 s (2 quadros); só uma nova mudança dispara de novo
    assert _run(detector, [c, c, c, c, a]) == ["nada", "nada", "nada", "nada", "mudou"]


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
    detector.capture_done()
    _run(detector, [b, c, c])  # durante a tradução, a tela virou c
    assert detector.freeze() is True  # o resultado é da tela b: descartar
    assert detector.changed_again() is True
    # c parou (agora por mais tempo, porque a tela se mostrou instável): traduz de novo
    events = _run(detector, [c] * 5)
    assert events[-1] == "parou" and "parou" not in events[:-1]


def test_tela_parada_durante_a_traducao_mostra_o_resultado():
    detector = ChangeDetector()
    a, b = _page(1), _page(2)
    _run(detector, [a, b, b, b])
    detector.capture_done()
    _run(detector, [b, b, _with_clock(b, 3)])
    assert detector.freeze() is False


def test_pedido_manual_compara_com_a_tela_do_comeco():
    detector = ChangeDetector()
    a, b = _page(1), _page(2)
    _run(detector, [a, a])
    detector.busy()  # atalho apertado: sem quadro "parado" de referência, vale o primeiro depois da captura
    _run(detector, [b, b])  # antes da captura (a tradução anterior ainda sumindo): ignorado
    detector.capture_done()
    _run(detector, [a, b])
    assert detector.freeze() is True


def test_pedido_manual_ignora_a_traducao_anterior_sumindo():
    detector = ChangeDetector()
    a, with_old_overlay = _page(1), _page(4)
    detector.busy()
    _run(detector, [with_old_overlay])
    detector.capture_done()
    _run(detector, [a, a])
    assert detector.freeze() is False


def test_tela_instavel_espera_mais_a_cada_descarte_e_desiste():
    detector = ChangeDetector()
    a, b = _page(1), _page(2)
    _run(detector, [a, b, b, b])  # parou: traduzindo
    for retry in range(1, 4):
        detector.capture_done()
        _run(detector, [_page(10 + retry)])  # mudou durante a tradução
        assert detector.freeze() is True
        assert detector.changed_again() is True
        # cada tentativa exige mais quadros parados: 2 x (1 + tentativas)
        frame = _page(20 + retry)
        events = _run(detector, [frame] * (2 * (1 + retry) + 1))
        assert events[-1] == "parou" and "parou" not in events[:-1]
    detector.capture_done()
    _run(detector, [_page(99)])
    assert detector.freeze() is True
    assert detector.changed_again() is False  # desiste até a próxima mudança
    detector.rearm()
    assert _run(detector, [a, a, a, b]) == ["nada", "nada", "nada", "mudou"]


def _cover(frame, box, value=255):
    """A tradução desenhada por cima de um balão (área da miniatura coberta)."""
    copy = {k: v.copy() for k, v in frame.items()}
    x0, y0, x1, y1 = box
    copy["tela"][y0:y1, x0:x1] = value
    return copy


BALAO = (40, 30, 120, 90)  # área da tradução na miniatura (~12% da tela)


def test_tradução_desenhada_nao_conta_como_mudanca():
    detector = ChangeDetector()
    a, b = _page(1), _page(2)
    _run(detector, [a, b, b, b])  # parou em b; traduzindo
    detector.freeze()
    detector.displayed({"tela": [BALAO]})
    with_translation = _cover(b, BALAO)
    assert _run(detector, [with_translation] * 6) == ["nada"] * 6
    assert detector.state == "observando" and not detector.legacy


def test_troca_de_pagina_logo_depois_de_mostrar():
    """O leitor virou a página assim que a tradução apareceu (antes de a sobreposição chegar à captura)."""
    detector = ChangeDetector()
    a, b, c = _page(1), _page(2), _page(3)
    _run(detector, [a, b, b, b])
    detector.freeze()
    detector.displayed({"tela": [BALAO]})
    events = _run(detector, [c, c, c])
    assert "mudou" in events


def test_sobreposicao_nas_capturas_vira_modo_antigo_sem_laco():
    """Onde a sobreposição escurece a captura inteira (Windows sem exclusão da captura, em VM)."""
    detector = ChangeDetector()
    a, b = _page(1), _page(2)
    darkened = {k: (v * 0.6).astype(np.uint8) for k, v in b.items()}
    _run(detector, [a, b, b, b])
    detector.freeze()
    detector.displayed({"tela": [BALAO]})
    assert "mudou" in _run(detector, [darkened] * 3)  # 1ª vez: pode ser uma troca de página real
    _run(detector, [b, b, b])  # a tradução sumiu; a tela parou: traduz de novo
    detector.freeze()
    detector.displayed({"tela": [BALAO]})
    assert _run(detector, [darkened] * 6) == ["nada"] * 6  # 2ª vez: é a sobreposição; nada de laço
    assert detector.legacy


def test_barra_de_tarefas_sumindo_nao_conta():
    """Windows: a barra de tarefas (fora da área útil) some quando a sobreposição aparece."""
    detector = ChangeDetector()
    areas = {"tela": (0.0, 0.0, 1.0, 0.94)}  # barra de tarefas nos 6% de baixo
    page = _page(1)
    no_taskbar = {k: v.copy() for k, v in page.items()}
    no_taskbar["tela"][151:, :] = 0
    frames = [only_available(f, areas) for f in [page, page, no_taskbar, page, no_taskbar]]
    assert _run(detector, frames) == ["nada"] * 5
    assert only_available(page, areas)["tela"].shape == page["tela"].shape


def test_flash_da_captura_no_gnome_nao_conta():
    detector = ChangeDetector()
    a, b = _page(1), _page(2)
    flash = {k: np.full_like(v, 255) for k, v in b.items()}
    _run(detector, [a, b, b, b])
    _run(detector, [flash])  # antes de a captura voltar
    detector.capture_done(settle_frames=2)
    _run(detector, [flash, b, b, b])  # o flash ainda some no primeiro quadro depois
    assert detector.freeze() is False
