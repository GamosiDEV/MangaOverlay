"""Banco local (SQLite): obras e traduções salvas.

Uma tradução é encontrada pelo texto lido pelo OCR (normalizado), e não pela aparência do recorte:
assim a mesma página é reconhecida mesmo capturada com outro zoom ou em outra posição da tela.
Traduções sem obra escolhida ficam com obra_id NULL ("sem obra").

O esquema é versionado (PRAGMA user_version): cada fase do plano acrescenta suas tabelas em _MIGRATIONS.
"""

import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir

from . import APP_NAME

DB_FILE = Path(user_data_dir(APP_NAME, appauthor=False)) / "mangaoverlay.db"

_MIGRATIONS = [
    # 1: obras e traduções
    """
    CREATE TABLE obras (
        id INTEGER PRIMARY KEY,
        nome TEXT NOT NULL UNIQUE COLLATE NOCASE,
        idioma_origem TEXT NOT NULL,
        criada_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE traducoes (
        id INTEGER PRIMARY KEY,
        obra_id INTEGER REFERENCES obras(id) ON DELETE CASCADE,
        texto_original TEXT NOT NULL,
        idioma_origem TEXT NOT NULL,
        idioma_destino TEXT NOT NULL,
        motor TEXT NOT NULL,
        modelo TEXT NOT NULL,
        traducao TEXT NOT NULL,
        criada_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    -- IFNULL: no SQLite, NULLs não colidem em índices únicos; "sem obra" precisa ser uma chave só
    CREATE UNIQUE INDEX traducoes_chave ON traducoes (
        IFNULL(obra_id, 0), texto_original, idioma_origem, idioma_destino, motor, modelo
    );
    """,
]


@dataclass(frozen=True)
class Work:
    id: int
    name: str
    source_lang: str


@dataclass(frozen=True)
class TranslationKey:
    """O que identifica uma tradução além do texto: obra, idiomas e motor/modelo usados."""

    work_id: int | None
    source: str
    target: str
    engine: str
    model: str


def normalize(text: str) -> str:
    """Forma canônica do texto lido pelo OCR: NFKC (largura dos caracteres) e sem espaços."""
    return "".join(unicodedata.normalize("NFKC", text).split())


class Database:
    """Uma conexão compartilhada entre a interface e a thread de trabalho, protegida por trava."""

    def __init__(self, path: Path = DB_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for number, script in enumerate(_MIGRATIONS[version:], start=version + 1):
                self._conn.executescript(f"BEGIN; {script}; PRAGMA user_version = {number}; COMMIT;")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- obras ------------------------------------------------------------------

    def works(self) -> list[Work]:
        with self._lock:
            rows = self._conn.execute("SELECT id, nome, idioma_origem FROM obras ORDER BY nome COLLATE NOCASE").fetchall()
        return [Work(*row) for row in rows]

    def work(self, work_id: int | None) -> Work | None:
        if work_id is None:
            return None
        with self._lock:
            row = self._conn.execute("SELECT id, nome, idioma_origem FROM obras WHERE id = ?", (work_id,)).fetchone()
        return Work(*row) if row else None

    def create_work(self, name: str, source_lang: str) -> Work:
        """Cria a obra; se já existir uma com o mesmo nome (ignorando maiúsculas), devolve a existente."""
        name = name.strip()
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO obras (nome, idioma_origem) VALUES (?, ?)", (name, source_lang))
            row = self._conn.execute("SELECT id, nome, idioma_origem FROM obras WHERE nome = ?", (name,)).fetchone()
        return Work(*row)

    def set_work_language(self, work_id: int, source_lang: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE obras SET idioma_origem = ? WHERE id = ?", (source_lang, work_id))

    # --- traduções --------------------------------------------------------------

    def find_translations(self, key: TranslationKey, texts: list[str]) -> dict[str, str]:
        """Traduções já salvas para os textos (normalizados), pelo texto."""
        wanted = {normalize(t) for t in texts if t.strip()}
        if not wanted:
            return {}
        found: dict[str, str] = {}
        items = list(wanted)
        with self._lock:
            for start in range(0, len(items), 500):  # limite de parâmetros do SQLite
                chunk = items[start : start + 500]
                marks = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"""SELECT texto_original, traducao FROM traducoes
                        WHERE IFNULL(obra_id, 0) = ? AND idioma_origem = ? AND idioma_destino = ?
                          AND motor = ? AND modelo = ? AND texto_original IN ({marks})""",
                    (key.work_id or 0, key.source, key.target, key.engine, key.model, *chunk),
                ).fetchall()
                found.update(rows)
        return found

    def save_translations(self, key: TranslationKey, pairs: list[tuple[str, str]]) -> None:
        """Grava (texto original, tradução); substitui se já havia tradução para a mesma chave."""
        rows = [
            (key.work_id, normalize(original), key.source, key.target, key.engine, key.model, translation)
            for original, translation in pairs
            if original.strip() and translation.strip()
        ]
        if not rows:
            return
        with self._lock:
            self._conn.execute("BEGIN")
            self._conn.executemany(
                """INSERT OR REPLACE INTO traducoes
                   (obra_id, texto_original, idioma_origem, idioma_destino, motor, modelo, traducao)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            self._conn.execute("COMMIT")

    def forget_translations(self, work_id: int | None) -> int:
        """Apaga as traduções salvas de uma obra (ou as "sem obra"). Retorna quantas foram apagadas."""
        with self._lock:
            cursor = self._conn.execute("DELETE FROM traducoes WHERE IFNULL(obra_id, 0) = ?", (work_id or 0,))
        return cursor.rowcount

    def count_translations(self, work_id: int | None) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM traducoes WHERE IFNULL(obra_id, 0) = ?", (work_id or 0,)).fetchone()
        return row[0]
