"""Importação em segundo plano: detecção e OCR das páginas pendentes, gravando o texto de cada balão no banco.

Roda numa thread própria, uma página por vez. O estado de cada página fica no banco, então parar,
fechar o app ou desligar o computador no meio não perde nada: a próxima execução continua das
páginas que ainda estão pendentes.
"""

import threading
import time
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, Signal

from .config import Config
from .db import Database
from .pipeline import Pipeline
from .sources import load_page


@dataclass
class ImportProgress:
    done: int
    total: int
    chapter: str
    page: int
    texts: int  # falas lidas até agora
    errors: int
    seconds_left: float | None
    blank: int = 0


@dataclass
class ImportSummary:
    pages: int
    texts: int
    errors: int
    cancelled: bool
    blank: int = 0  # páginas em branco (arquivo de prévia, páginas separadoras)


class Importer(QObject):
    progress = Signal(object)  # ImportProgress
    finished = Signal(object)  # ImportSummary

    def __init__(self, db: Database, pipeline: Pipeline, parent: QObject | None = None):
        super().__init__(parent)
        self._db = db
        self._pipeline = pipeline
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: Config) -> bool:
        """Processa todas as páginas pendentes (de todas as obras). False se já estiver rodando."""
        if self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(replace(config),), name="importacao", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Para depois da página atual; o que faltou continua pendente no banco."""
        self._stop.set()

    def wait(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, config: Config) -> None:
        pages = self._db.pending_pages()
        total, texts, errors, blank = len(pages), 0, 0, 0
        started = time.perf_counter()
        done = 0
        for done, page in enumerate(pages, start=1):
            if self._stop.is_set():
                done -= 1
                break
            try:
                image = load_page(page.origin, page.file)
                if _is_blank(image):
                    self._db.mark_page_blank(page.id)
                    blank += 1
                    regions = None
                else:
                    # Cada obra usa o próprio idioma de origem
                    regions = self._pipeline.read_page(image, replace(config, source_lang=page.source_lang))
            except Exception as exc:  # arquivo movido/corrompido, falha no modelo: marca e segue para a próxima
                self._db.mark_page_error(page.id, str(exc) or exc.__class__.__name__)
                errors += 1
            else:
                if regions is not None:
                    self._db.save_page_texts(page.id, regions)
                    texts += len(regions)
            elapsed = time.perf_counter() - started
            seconds_left = elapsed / done * (total - done) if done >= 3 else None
            self.progress.emit(ImportProgress(done, total, page.chapter, page.number, texts, errors, seconds_left, blank))
        self.finished.emit(ImportSummary(done, texts, errors, cancelled=self._stop.is_set(), blank=blank))


def _is_blank(image) -> bool:
    """Página de uma cor só (sem desenho nem texto): não vale rodar o detector nem o OCR."""
    import numpy as np

    small = np.asarray(image.convert("L").resize((200, 280)), dtype=np.float32)
    return float(small.std()) < 2.0
