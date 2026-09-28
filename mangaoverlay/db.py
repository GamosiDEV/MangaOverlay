"""Banco local (SQLite): obras e traduções salvas.

Uma tradução é encontrada pelo texto lido pelo OCR (normalizado), e não pela aparência do recorte:
assim a mesma página é reconhecida mesmo capturada com outro zoom ou em outra posição da tela.
Traduções sem obra escolhida ficam com obra_id NULL ("sem obra").

O esquema é versionado (PRAGMA user_version): cada fase do plano acrescenta suas tabelas em _MIGRATIONS.
"""

import sqlite3
import threading
import unicodedata
from difflib import SequenceMatcher
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
    # 2: capítulos importados, páginas (com estado, para retomar) e o texto lido de cada balão.
    # As imagens não são copiadas: `origem` + `arquivo` apontam para o original.
    """
    CREATE TABLE capitulos (
        id INTEGER PRIMARY KEY,
        obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
        nome TEXT NOT NULL,
        ordem REAL NOT NULL,
        origem TEXT NOT NULL,
        criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (obra_id, origem)
    );
    CREATE TABLE paginas (
        id INTEGER PRIMARY KEY,
        capitulo_id INTEGER NOT NULL REFERENCES capitulos(id) ON DELETE CASCADE,
        numero INTEGER NOT NULL,
        arquivo TEXT NOT NULL,
        estado TEXT NOT NULL DEFAULT 'pendente' CHECK (estado IN ('pendente', 'lida', 'erro')),
        erro TEXT,
        UNIQUE (capitulo_id, numero)
    );
    CREATE INDEX paginas_estado ON paginas (estado);
    CREATE TABLE regioes (
        id INTEGER PRIMARY KEY,
        pagina_id INTEGER NOT NULL REFERENCES paginas(id) ON DELETE CASCADE,
        ordem INTEGER NOT NULL,
        x0 INTEGER NOT NULL, y0 INTEGER NOT NULL, x1 INTEGER NOT NULL, y1 INTEGER NOT NULL,
        texto_original TEXT NOT NULL
    );
    CREATE INDEX regioes_pagina ON regioes (pagina_id);
    """,
    # 3: lista de personagens por obra e marca dos capítulos cujos nomes já foram levantados
    """
    CREATE TABLE personagens (
        id INTEGER PRIMARY KEY,
        obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
        nome TEXT NOT NULL,
        nome_original TEXT NOT NULL DEFAULT '',
        genero TEXT NOT NULL DEFAULT '',
        jeito_de_falar TEXT NOT NULL DEFAULT '',
        notas TEXT NOT NULL DEFAULT '',
        UNIQUE (obra_id, nome COLLATE NOCASE)
    );
    ALTER TABLE capitulos ADD COLUMN nomes_levantados INTEGER NOT NULL DEFAULT 0;
    """,
    # 4: tradução em lote. Cada requisição cobre um bloco de páginas; as falas enviadas são calculadas na
    # hora (só o que ainda não tem tradução), então retomar ou reenviar nunca paga duas vezes a mesma fala.
    """
    CREATE TABLE lotes (
        id INTEGER PRIMARY KEY,
        obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
        motor TEXT NOT NULL,
        modelo TEXT NOT NULL,
        idioma_origem TEXT NOT NULL,
        idioma_destino TEXT NOT NULL,
        paginas_por_bloco INTEGER NOT NULL,
        estado TEXT NOT NULL DEFAULT 'ativo' CHECK (estado IN ('ativo', 'pausado', 'concluido', 'cancelado')),
        erro TEXT,
        custo_estimado REAL,
        criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE requisicoes (
        id INTEGER PRIMARY KEY,
        lote_id INTEGER NOT NULL REFERENCES lotes(id) ON DELETE CASCADE,
        ordem INTEGER NOT NULL,
        estado TEXT NOT NULL DEFAULT 'pendente' CHECK (estado IN ('pendente', 'concluida', 'falhou')),
        tentativas INTEGER NOT NULL DEFAULT 0,
        erro TEXT,
        tokens_entrada INTEGER NOT NULL DEFAULT 0,
        tokens_cache INTEGER NOT NULL DEFAULT 0,
        tokens_saida INTEGER NOT NULL DEFAULT 0,
        custo REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE requisicao_paginas (
        requisicao_id INTEGER NOT NULL REFERENCES requisicoes(id) ON DELETE CASCADE,
        pagina_id INTEGER NOT NULL REFERENCES paginas(id) ON DELETE CASCADE,
        PRIMARY KEY (requisicao_id, pagina_id)
    );
    """,
]


@dataclass(frozen=True)
class Work:
    id: int
    name: str
    source_lang: str


@dataclass(frozen=True)
class PendingPage:
    """Página importada que ainda não passou pela detecção e OCR."""

    id: int
    work_id: int
    source_lang: str
    chapter: str
    origin: str
    number: int
    file: str


@dataclass(frozen=True)
class ChapterSummary:
    id: int
    name: str
    pages: int
    read: int
    errors: int
    texts: int


@dataclass(frozen=True)
class Character:
    name: str  # como aparece na tradução (obrigatório)
    original: str = ""  # como aparece no original (opcional)
    gender: str = ""  # "masculino", "feminino", "outro" ou vazio
    speech: str = ""  # jeito de falar
    notes: str = ""


@dataclass(frozen=True)
class ChapterTexts:
    id: int
    name: str
    texts: list[str]


@dataclass(frozen=True)
class SourceLine:
    """Uma fala de capítulo importado, com a localização (para marcar mudança de página no pedido)."""

    chapter: str
    page: int
    text: str


@dataclass(frozen=True)
class Batch:
    id: int
    work_id: int
    engine: str
    model: str
    source_lang: str
    target_lang: str
    pages_per_block: int
    state: str
    error: str | None
    estimated_cost: float | None


@dataclass(frozen=True)
class BatchRequest:
    id: int
    order: int
    state: str
    attempts: int


@dataclass(frozen=True)
class BatchSummary:
    requests: int
    done: int
    failed: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost: float


@dataclass(frozen=True)
class TranslationKey:
    """O que identifica uma tradução além do texto: obra, idiomas e motor/modelo usados."""

    work_id: int | None
    source: str
    target: str
    engine: str
    model: str


# Busca aproximada: o OCR às vezes lê um risco do desenho como um caractere a mais ou diferente
# (「一宿題…」 x 「・宿題…」). Falas curtas ficam de fora: nelas uma letra muda o sentido (はい x はあ).
FUZZY_MIN_LENGTH = 4
FUZZY_MIN_RATIO = 0.8


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

    def find_translations(
        self, key: TranslationKey, texts: list[str], also_engines: tuple[str, ...] = (), fuzzy: bool = True
    ) -> dict[str, str]:
        """Traduções já salvas para os textos, indexadas pelo texto normalizado.

        Primeiro pelo texto exato; o que faltar, por semelhança com os textos já traduzidos na mesma chave.
        `also_engines`: se ainda faltar, aceita traduções desses motores (qualquer modelo) na mesma obra e idiomas
        (ex.: ler com o modo visão uma obra traduzida em lote com o mesmo provedor no modo texto).
        """
        found = self._find_exact(key, texts)
        missing = {normalize(t) for t in texts if t.strip()} - found.keys()
        if fuzzy and {t for t in missing if len(t) >= FUZZY_MIN_LENGTH}:
            found.update(self._find_similar(key, {t for t in missing if len(t) >= FUZZY_MIN_LENGTH}))
        for engine in also_engines:
            missing = {normalize(t) for t in texts if t.strip()} - found.keys()
            if not missing:
                break
            for model in self._models_for(key, engine):
                other = TranslationKey(key.work_id, key.source, key.target, engine, model)
                found.update(self.find_translations(other, sorted(missing - found.keys())))
        return found

    def _models_for(self, key: TranslationKey, engine: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT modelo FROM traducoes WHERE IFNULL(obra_id, 0) = ? AND idioma_origem = ? AND idioma_destino = ?
                   AND motor = ? GROUP BY modelo ORDER BY MAX(id) DESC""",
                (key.work_id or 0, key.source, key.target, engine),
            ).fetchall()
        return [row[0] for row in rows]

    def _find_similar(self, key: TranslationKey, texts: set[str]) -> dict[str, str]:
        with self._lock:
            candidates = self._conn.execute(
                """SELECT texto_original, traducao FROM traducoes
                   WHERE IFNULL(obra_id, 0) = ? AND idioma_origem = ? AND idioma_destino = ? AND motor = ? AND modelo = ?
                     AND length(texto_original) >= ?""",
                (key.work_id or 0, key.source, key.target, key.engine, key.model, FUZZY_MIN_LENGTH),
            ).fetchall()
        found = {}
        for text in texts:
            best, best_ratio = None, FUZZY_MIN_RATIO
            for original, translation in candidates:
                if abs(len(original) - len(text)) > max(2, len(text) // 3):
                    continue
                matcher = SequenceMatcher(None, text, original, autojunk=False)
                if matcher.quick_ratio() < best_ratio:
                    continue
                ratio = matcher.ratio()
                if ratio >= best_ratio:
                    best, best_ratio = translation, ratio
            if best is not None:
                found[text] = best
        return found

    def _find_exact(self, key: TranslationKey, texts: list[str]) -> dict[str, str]:
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

    # --- capítulos importados ---------------------------------------------------

    def add_chapter(self, work_id: int, name: str, order: float, origin: str, files: list[str]) -> int | None:
        """Registra o capítulo e suas páginas como pendentes. None se essa origem já foi importada na obra."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO capitulos (obra_id, nome, ordem, origem) VALUES (?, ?, ?, ?)",
                    (work_id, name, order, origin),
                )
                if cursor.rowcount == 0:
                    self._conn.execute("ROLLBACK")
                    return None
                chapter_id = cursor.lastrowid
                self._conn.executemany(
                    "INSERT INTO paginas (capitulo_id, numero, arquivo) VALUES (?, ?, ?)",
                    [(chapter_id, number, file) for number, file in enumerate(files, start=1)],
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return chapter_id

    def pending_pages(self, work_id: int | None = None) -> list[PendingPage]:
        """Páginas ainda não lidas, na ordem de leitura (obra, capítulo, página). Todas as obras se work_id for None."""
        query = """SELECT p.id, o.id, o.idioma_origem, c.nome, c.origem, p.numero, p.arquivo
                   FROM paginas p JOIN capitulos c ON c.id = p.capitulo_id JOIN obras o ON o.id = c.obra_id
                   WHERE p.estado = 'pendente'"""
        params: tuple = ()
        if work_id is not None:
            query += " AND o.id = ?"
            params = (work_id,)
        query += " ORDER BY o.id, c.ordem, c.nome, p.numero"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [PendingPage(*row) for row in rows]

    def save_page_texts(self, page_id: int, regions: list[tuple[tuple[int, int, int, int], str]]) -> None:
        """Grava o texto lido de cada balão (na ordem de leitura) e marca a página como lida.

        Substitui o que houver: reprocessar uma página nunca duplica balões.
        """
        with self._lock:
            self._conn.execute("BEGIN")
            self._conn.execute("DELETE FROM regioes WHERE pagina_id = ?", (page_id,))
            self._conn.executemany(
                "INSERT INTO regioes (pagina_id, ordem, x0, y0, x1, y1, texto_original) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(page_id, order, *box, text) for order, (box, text) in enumerate(regions, start=1)],
            )
            self._conn.execute("UPDATE paginas SET estado = 'lida', erro = NULL WHERE id = ?", (page_id,))
            self._conn.execute("COMMIT")

    def mark_page_error(self, page_id: int, message: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE paginas SET estado = 'erro', erro = ? WHERE id = ?", (message, page_id))

    def retry_errors(self, work_id: int) -> int:
        """Volta para pendente as páginas com erro da obra (ex.: arquivo que estava fora do lugar)."""
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE paginas SET estado = 'pendente', erro = NULL
                   WHERE estado = 'erro' AND capitulo_id IN (SELECT id FROM capitulos WHERE obra_id = ?)""",
                (work_id,),
            )
        return cursor.rowcount

    def chapters(self, work_id: int) -> list[ChapterSummary]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.id, c.nome, COUNT(p.id),
                          SUM(p.estado = 'lida'), SUM(p.estado = 'erro'),
                          (SELECT COUNT(*) FROM regioes r JOIN paginas p2 ON p2.id = r.pagina_id WHERE p2.capitulo_id = c.id)
                   FROM capitulos c LEFT JOIN paginas p ON p.capitulo_id = c.id
                   WHERE c.obra_id = ? GROUP BY c.id ORDER BY c.ordem, c.nome""",
                (work_id,),
            ).fetchall()
        return [ChapterSummary(r[0], r[1], r[2], r[3] or 0, r[4] or 0, r[5]) for r in rows]

    # --- personagens ------------------------------------------------------------

    def characters(self, work_id: int | None) -> list[Character]:
        if work_id is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT nome, nome_original, genero, jeito_de_falar, notas FROM personagens WHERE obra_id = ? ORDER BY id",
                (work_id,),
            ).fetchall()
        return [Character(*row) for row in rows]

    def save_characters(self, work_id: int, characters: list[Character]) -> None:
        """Substitui a lista da obra (a tela da lista sempre edita a lista inteira)."""
        unique: dict[str, Character] = {}
        for character in characters:
            if character.name.strip():
                unique.setdefault(character.name.strip().casefold(), character)
        with self._lock:
            self._conn.execute("BEGIN")
            self._conn.execute("DELETE FROM personagens WHERE obra_id = ?", (work_id,))
            self._conn.executemany(
                """INSERT INTO personagens (obra_id, nome, nome_original, genero, jeito_de_falar, notas)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (work_id, c.name.strip(), c.original.strip(), c.gender.strip(), c.speech.strip(), c.notes.strip())
                    for c in unique.values()
                ],
            )
            self._conn.execute("COMMIT")

    def chapter_texts(self, work_id: int, only_pending_names: bool) -> list[ChapterTexts]:
        """Texto lido de cada capítulo importado (já passado pelo OCR), em ordem de leitura."""
        query = """SELECT c.id, c.nome, r.texto_original FROM capitulos c
                   JOIN paginas p ON p.capitulo_id = c.id JOIN regioes r ON r.pagina_id = p.id
                   WHERE c.obra_id = ?"""
        if only_pending_names:
            query += " AND c.nomes_levantados = 0"
        query += " ORDER BY c.ordem, c.nome, p.numero, r.ordem"
        with self._lock:
            rows = self._conn.execute(query, (work_id,)).fetchall()
        chapters: dict[int, ChapterTexts] = {}
        for chapter_id, name, text in rows:
            chapters.setdefault(chapter_id, ChapterTexts(chapter_id, name, [])).texts.append(text)
        return list(chapters.values())

    def mark_names_surveyed(self, chapter_ids: list[int]) -> None:
        with self._lock:
            self._conn.executemany("UPDATE capitulos SET nomes_levantados = 1 WHERE id = ?", [(i,) for i in chapter_ids])

    def read_texts(self, work_id: int) -> list[str]:
        """Todo texto original conhecido da obra: capítulos importados e falas traduzidas na tela."""
        with self._lock:
            imported = self._conn.execute(
                """SELECT r.texto_original FROM regioes r JOIN paginas p ON p.id = r.pagina_id
                   JOIN capitulos c ON c.id = p.capitulo_id WHERE c.obra_id = ?""",
                (work_id,),
            ).fetchall()
            screen = self._conn.execute("SELECT texto_original FROM traducoes WHERE obra_id = ?", (work_id,)).fetchall()
        return [row[0] for row in imported + screen]

    # --- tradução em lote ------------------------------------------------------------

    def chapter_lines(self, chapter_ids: list[int]) -> dict[int, list[tuple[int, str]]]:
        """Por capítulo: (página, texto) de cada fala lida, em ordem de leitura."""
        if not chapter_ids:
            return {}
        marks = ",".join("?" * len(chapter_ids))
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT c.id, p.id, r.texto_original FROM capitulos c JOIN paginas p ON p.capitulo_id = c.id
                    JOIN regioes r ON r.pagina_id = p.id WHERE c.id IN ({marks})
                    ORDER BY c.ordem, c.nome, p.numero, r.ordem""",
                chapter_ids,
            ).fetchall()
        result: dict[int, list[tuple[int, str]]] = {}
        for chapter_id, page_id, text in rows:
            result.setdefault(chapter_id, []).append((page_id, text))
        return result

    def chapter_pages(self, chapter_ids: list[int]) -> list[int]:
        """Páginas lidas dos capítulos, na ordem de leitura."""
        if not chapter_ids:
            return []
        marks = ",".join("?" * len(chapter_ids))
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT p.id FROM paginas p JOIN capitulos c ON c.id = p.capitulo_id
                    WHERE c.id IN ({marks}) AND p.estado = 'lida' ORDER BY c.ordem, c.nome, p.numero""",
                chapter_ids,
            ).fetchall()
        return [row[0] for row in rows]

    def create_batch(
        self, work_id: int, key: TranslationKey, pages_per_block: int, page_ids: list[int], estimated_cost: float | None
    ) -> int:
        """Cria o lote e uma requisição pendente para cada bloco de páginas."""
        with self._lock:
            self._conn.execute("BEGIN")
            cursor = self._conn.execute(
                """INSERT INTO lotes (obra_id, motor, modelo, idioma_origem, idioma_destino, paginas_por_bloco, custo_estimado)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (work_id, key.engine, key.model, key.source, key.target, pages_per_block, estimated_cost),
            )
            batch_id = cursor.lastrowid
            for order, start in enumerate(range(0, len(page_ids), pages_per_block), start=1):
                request_id = self._conn.execute(
                    "INSERT INTO requisicoes (lote_id, ordem) VALUES (?, ?)", (batch_id, order)
                ).lastrowid
                self._conn.executemany(
                    "INSERT INTO requisicao_paginas (requisicao_id, pagina_id) VALUES (?, ?)",
                    [(request_id, page_id) for page_id in page_ids[start : start + pages_per_block]],
                )
            self._conn.execute("COMMIT")
        return batch_id

    def batches(self, states: tuple[str, ...] = ("ativo",)) -> list[Batch]:
        marks = ",".join("?" * len(states))
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT id, obra_id, motor, modelo, idioma_origem, idioma_destino, paginas_por_bloco, estado, erro,
                           custo_estimado FROM lotes WHERE estado IN ({marks}) ORDER BY id""",
                states,
            ).fetchall()
        return [Batch(*row) for row in rows]

    def batch_requests(self, batch_id: int, states: tuple[str, ...] = ("pendente",)) -> list[BatchRequest]:
        marks = ",".join("?" * len(states))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, ordem, estado, tentativas FROM requisicoes WHERE lote_id = ? AND estado IN ({marks}) ORDER BY ordem",
                (batch_id, *states),
            ).fetchall()
        return [BatchRequest(*row) for row in rows]

    def request_lines(self, request_id: int) -> list[SourceLine]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.nome, p.numero, r.texto_original FROM requisicao_paginas rp
                   JOIN paginas p ON p.id = rp.pagina_id JOIN capitulos c ON c.id = p.capitulo_id
                   JOIN regioes r ON r.pagina_id = p.id WHERE rp.requisicao_id = ?
                   ORDER BY c.ordem, c.nome, p.numero, r.ordem""",
                (request_id,),
            ).fetchall()
        return [SourceLine(*row) for row in rows]

    def record_request(
        self, request_id: int, state: str, usage: tuple[int, int, int], cost: float, error: str | None = None
    ) -> None:
        """Acumula o uso/custo (uma requisição pode precisar de mais de uma chamada) e grava o estado."""
        with self._lock:
            self._conn.execute(
                """UPDATE requisicoes SET estado = ?, erro = ?, tentativas = tentativas + 1,
                   tokens_entrada = tokens_entrada + ?, tokens_cache = tokens_cache + ?, tokens_saida = tokens_saida + ?,
                   custo = custo + ? WHERE id = ?""",
                (state, error, *usage, cost, request_id),
            )

    def set_batch_state(self, batch_id: int, state: str, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute("UPDATE lotes SET estado = ?, erro = ? WHERE id = ?", (state, error, batch_id))

    def retry_failed_requests(self, batch_id: int) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE requisicoes SET estado = 'pendente', erro = NULL WHERE lote_id = ? AND estado = 'falhou'", (batch_id,)
            )
        return cursor.rowcount

    def batch_summary(self, batch_id: int) -> BatchSummary:
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*), SUM(estado = 'concluida'), SUM(estado = 'falhou'), SUM(tokens_entrada),
                          SUM(tokens_cache), SUM(tokens_saida), SUM(custo) FROM requisicoes WHERE lote_id = ?""",
                (batch_id,),
            ).fetchone()
        return BatchSummary(*(value or 0 for value in row))
