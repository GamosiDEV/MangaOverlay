"""`--download-models`: baixa de uma vez todos os modelos locais.

O instalador usa isto para que o app funcione logo ao abrir; sem isso, cada modelo é baixado no primeiro uso.
Cada modelo é carregado na CPU pelo mesmo código do app (e descartado em seguida), então vêm exatamente
os arquivos que o app vai pedir, nos mesmos lugares.
"""

import gc
import sys
from collections.abc import Callable


def _steps() -> list[tuple[str, Callable[[], object]]]:
    from .colorize import Colorizer
    from .detector import Detector
    from .ocr import _EASYOCR_LANGS, OcrEngine
    from .translators import NllbTranslator

    steps: list[tuple[str, Callable[[], object]]] = [
        ("detector de balões", lambda: Detector("cpu")),
        ("OCR de japonês (manga-ocr)", lambda: OcrEngine("cpu").load("ja")),
    ]
    steps += [(f"OCR ({source}, EasyOCR)", lambda s=source: OcrEngine("cpu").load(s)) for source in _EASYOCR_LANGS]
    steps += [
        ("tradutor offline (NLLB)", lambda: NllbTranslator("cpu")),
        ("colorizador", lambda: Colorizer("cpu")),
    ]
    return steps


def download_all() -> int:
    failed = []
    steps = _steps()
    for number, (name, load) in enumerate(steps, 1):
        print(f"[{number}/{len(steps)}] {name}…", file=sys.stderr, flush=True)
        try:
            load()
        except Exception as exc:  # segue com os outros; o que faltar é baixado no primeiro uso
            print(f"    falhou: {exc.__class__.__name__}: {exc}", file=sys.stderr)
            failed.append(name)
        gc.collect()
    if failed:
        print(f"Não foi possível baixar: {', '.join(failed)}. Eles serão baixados no primeiro uso.", file=sys.stderr)
        return 1
    print("Modelos prontos.", file=sys.stderr)
    return 0


def delete_all() -> None:
    """Apaga do cache do Hugging Face os modelos do app (o desinstalador usa com --purge).
    O EasyOCR fica na pasta de cache do app, que o desinstalador apaga inteira."""
    from huggingface_hub import scan_cache_dir
    from huggingface_hub.errors import CacheNotFound

    from .colorize import MODEL_REPO
    from .detector import MODEL_ID
    from .ocr import MANGA_OCR_ID
    from .translators import NLLB_ID

    repos = {MODEL_ID, MANGA_OCR_ID, NLLB_ID, MODEL_REPO}
    try:
        cache = scan_cache_dir()
    except CacheNotFound:  # nenhum modelo baixado
        return
    revisions = [rev.commit_hash for repo in cache.repos if repo.repo_id in repos for rev in repo.revisions]
    cache.delete_revisions(*revisions).execute()
