"""`--image`: traduz (e/ou colore) um arquivo de imagem e grava o resultado (para testes e ajustes)."""

import os
import sys
import time
from pathlib import Path

from PIL import Image


def translate_file(
    path: str, out: str | None, engine: str | None = None, source: str | None = None, colorize: bool = False, translate: bool = True
) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication, QImage, QPainter

    from .config import ENGINES, Config
    from .pipeline import Pipeline, PipelineError
    from .render import base_font, paint_items
    from .screenshot import pil_to_qimage
    from .translators import TranslationError

    _app = QGuiApplication(sys.argv[:1])
    config = Config.load()
    if engine:
        if engine not in ENGINES:
            print(f"Motor desconhecido: {engine}. Opções: {', '.join(ENGINES)}", file=sys.stderr)
            return 2
        config.engine = engine
    if source:
        config.source_lang = source
    image = Image.open(path).convert("RGB")
    pipeline = Pipeline()

    started = time.perf_counter()
    try:
        result = pipeline.process(
            image, config, status=lambda message: print(message, file=sys.stderr), translate=translate, colorize=colorize
        )
    except (PipelineError, TranslationError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    elapsed = time.perf_counter() - started

    for item in result.items:
        print(f"{item.fill}  {item.original!r}\n    -> {item.text!r}")
    colored = f", página colorida em {result.color_box}" if result.color_image is not None else ""
    print(f"{len(result.items)} texto(s){colored} em {elapsed:.1f}s (motor: {config.engine})", file=sys.stderr)

    canvas = pil_to_qimage(image).convertToFormat(QImage.Format.Format_RGB32)
    painter = QPainter(canvas)
    if result.color_image is not None:
        painter.drawImage(result.color_box[0], result.color_box[1], pil_to_qimage(result.color_image))
    paint_items(painter, result.items, base_font(config.font_family))
    painter.end()
    target = out or str(Path(path).with_name(f"{Path(path).stem}-traduzido.png"))
    canvas.save(target)
    print(f"Gravado em {target}", file=sys.stderr)
    return 0
