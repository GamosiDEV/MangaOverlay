"""Diagnóstico do crash no Windows (docs/WINDOWS-CRASH.md): abre o app de verdade e traduz a tela em ciclos.

Mais rápido que o reproduzir-crash.ps1 (não espera o atalho nem precisa do teclado) e permite desligar partes
do app pela linha de comando, para achar qual delas, junto com o pipeline, provoca o crash.

    python tests\\windows\\app_ciclo.py [--vezes 6] [--intervalo 3] [--sem-aquecimento] [--sem-atalhos]
                                          [--sem-sobreposicao] [--imagem ARQUIVO] [--cpu]

Termina com "OK" se nenhuma tradução derrubou o processo (um crash nativo encerra com 0xC0000374/0xC0000005).
"""

import argparse
import faulthandler
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
faulthandler.enable(all_threads=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vezes", type=int, default=6)
    parser.add_argument("--intervalo", type=float, default=3.0, help="segundos entre o fim de uma tradução e a próxima")
    parser.add_argument("--sem-aquecimento", action="store_true", help="não carrega os modelos ao abrir")
    parser.add_argument("--sem-atalhos", action="store_true", help="não liga o pynput")
    parser.add_argument("--sem-sobreposicao", action="store_true", help="não mostra nada na tela")
    parser.add_argument("--sem-ipc", action="store_true", help="não abre o servidor de comandos (named pipe)")
    parser.add_argument("--sem-janela", action="store_true", help="nem cria a janela da sobreposição")
    parser.add_argument("--sem-banco", action="store_true", help="o pipeline não recebe o banco")
    parser.add_argument("--sem-lote", action="store_true", help="não cria o importador nem o executor de lotes")
    parser.add_argument("--imagem", help="usa este arquivo no lugar da captura de tela")
    parser.add_argument("--cpu", action="store_true")
    args = parser.parse_args()

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QGuiApplication

    from mangaoverlay import app as app_module
    from mangaoverlay import screenshot

    if args.sem_ipc:
        app_module.ipc.Server.start = lambda self: None
        app_module.ipc.Server.close = lambda self: None
    if args.sem_atalhos:
        app_module.HotkeyManager.apply = lambda self, sequences: None
    if args.sem_aquecimento:
        app_module.MangaOverlayApp._warm_up = lambda self: None
    if args.sem_sobreposicao:
        app_module.OverlayWindow.show_busy = lambda self: None
        app_module.OverlayWindow.show_result = lambda self, *a: None
    if args.sem_janela:
        app_module.MangaOverlayApp._overlay = lambda self, name: None
    if args.sem_banco:
        real_pipeline = app_module.Pipeline
        app_module.Pipeline = lambda db=None: real_pipeline(None)
    if args.sem_lote:

        class _Parado(app_module.QObject):
            progress = app_module.Signal(object)
            finished = app_module.Signal(object)
            log = app_module.Signal(object)
            running = False

            def __init__(self, *a):
                super().__init__()

            def stop(self): ...
            def interrupt(self): ...
            def pause(self): ...
            def wait(self, timeout): ...
            def start(self, config):
                return False

        app_module.Importer = app_module.BatchRunner = _Parado
    if args.imagem:
        from PIL import Image

        picture = Image.open(args.imagem).convert("RGB")
        screenshot.grab_qt_screens = lambda: {QGuiApplication.primaryScreen().name(): picture.copy()}

    done = {"n": 0}
    original = app_module.MangaOverlayApp._on_processed

    def on_processed(self, results, translate, colorize):
        original(self, results, translate, colorize)
        done["n"] += 1
        texts = sum(len(r.items) for r, _size in results.values())
        print(f"  {done['n']}/{args.vezes}: {texts} texto(s)", flush=True)
        if done["n"] >= args.vezes:
            print("OK: nenhum crash", flush=True)
            QTimer.singleShot(500, self.quit)
        else:
            QTimer.singleShot(int(args.intervalo * 1000), self.translate_screen)

    app_module.MangaOverlayApp._on_processed = on_processed

    def on_failed(self, exc):
        print(f"  falhou: {exc!r}", flush=True)
        self._busy = False

    app_module.MangaOverlayApp._on_failed = on_failed

    real_run = app_module.MangaOverlayApp.__init__

    def init(self, qapp):
        real_run(self, qapp)
        if args.cpu:
            self.config = replace(self.config, use_gpu=False)
        # Espera o aquecimento (na fila de trabalho) e começa
        QTimer.singleShot(1000, self.translate_screen)

    app_module.MangaOverlayApp.__init__ = init
    print(
        "Ciclo do app:", ", ".join(k for k, v in vars(args).items() if v is True) or "app completo",
        f"· {args.vezes} tradução(ões)", flush=True,
    )
    return app_module.run()


if __name__ == "__main__":
    raise SystemExit(main())
