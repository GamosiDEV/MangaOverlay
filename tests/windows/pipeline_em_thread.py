"""Diagnóstico do crash no Windows (docs/WINDOWS-CRASH.md): em que tipo de thread o pipeline quebra?

Roda o pipeline (detecção + OCR + NLLB) na página de teste várias vezes, num dos modos:

    principal   na thread principal, sem Qt rodando (como o --image, que nunca quebrou)
    thread      numa threading.Thread do Python, sem Qt rodando
    qt-thread   numa threading.Thread, com um QApplication e o loop de eventos rodando
    pool        num QThreadPool com o loop de eventos rodando (como o app)

Uso, da pasta do repositório, com o python.exe do ambiente instalado:

    %LOCALAPPDATA%\\Programs\\MangaOverlay\\.venv\\Scripts\\python.exe tests\\windows\\pipeline_em_thread.py pool
    ... pipeline_em_thread.py thread --vezes 10 --cpu --uma-thread

Um crash nativo derruba o processo com o código 0xC0000374 (o faulthandler mostra as pilhas).
Se terminar com "OK", o modo não quebrou.
"""

import argparse
import faulthandler
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
faulthandler.enable(all_threads=True)
if "--qt-primeiro" in sys.argv:
    # Como o app: o PySide6 (com o próprio runtime do Visual C++) carrega antes do PyTorch
    import PySide6.QtWidgets  # noqa: F401


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("modo", choices=["principal", "thread", "qt-thread", "pool"])
    parser.add_argument("--vezes", type=int, default=6)
    parser.add_argument("--cpu", action="store_true", help="não usa a GPU")
    parser.add_argument("--uma-thread", action="store_true", help="torch.set_num_threads(1) (testa conflito de OpenMP)")
    parser.add_argument("--qt-primeiro", action="store_true", help="importa o PySide6 antes do PyTorch, como o app")
    args = parser.parse_args()

    from PIL import Image

    from mangaoverlay.config import Config
    from mangaoverlay.pipeline import Pipeline

    if args.uma_thread:
        import torch

        torch.set_num_threads(1)
    image = Image.open(ROOT / "tests" / "fixtures" / "pagina-sintetica.png").convert("RGB")
    config = Config(engine="local", source_lang="ja", use_gpu=not args.cpu)
    pipeline = Pipeline()
    done = threading.Event()

    def work() -> None:
        try:
            for n in range(1, args.vezes + 1):
                started = time.perf_counter()
                result = pipeline.process(image, config)
                print(f"  {n}/{args.vezes}: {len(result.items)} fala(s) em {time.perf_counter() - started:.1f} s", flush=True)
        finally:
            done.set()

    print(f"Modo {args.modo}, {args.vezes} vez(es), {'CPU' if args.cpu else 'GPU se houver'}"
          f"{', torch com 1 thread' if args.uma_thread else ''}", flush=True)
    if args.modo == "principal":
        work()
    elif args.modo == "thread":
        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
    else:
        os.environ.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "offscreen")
        from PySide6.QtCore import QThreadPool, QTimer
        from PySide6.QtWidgets import QApplication

        app = QApplication(sys.argv[:1])
        pool = QThreadPool()
        pool.setMaxThreadCount(1)
        if args.modo == "pool":
            pool.start(work)
        else:
            threading.Thread(target=work, daemon=True).start()
        timer = QTimer()
        timer.timeout.connect(lambda: done.is_set() and app.quit())
        timer.start(200)
        app.exec()
    print("OK: nenhum crash", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
