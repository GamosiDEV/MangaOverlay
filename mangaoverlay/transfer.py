"""Exportar e importar dados: obras, capítulos importados (com o texto lido de cada balão) e traduções.

O arquivo é um .zip com um dados.json versionado, com qualquer combinação de três partes das obras escolhidas:
- "obras": idioma, personagens e memória (resumo e glossário);
- "paginas": capítulos importados, páginas, posição e texto de cada balão;
- "traducoes": traduções salvas, de todos os motores (e, opcionalmente, as feitas sem obra).
O nome e o idioma de cada obra vão sempre: páginas e traduções precisam de uma obra onde entrar. Não vão as imagens
(o banco só guarda onde elas estão) nem os lotes de tradução (estado de execução).

A importação mescla sem sobrescrever nada: uma obra com o mesmo nome recebe o que faltar; capítulos já existentes
(mesma origem), traduções, personagens e termos repetidos são ignorados. Tudo numa transação só.
"""

import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__
from .db import Database

FORMAT = "mangaoverlay"
VERSION = 1
_DATA_FILE = "dados.json"
PARTS = {"obras": "Obras (idioma, personagens e memória)", "paginas": "Capítulos e páginas (texto lido de cada balão)",
         "traducoes": "Traduções"}
_TRANSLATION_FIELDS = ["texto_original", "idioma_origem", "idioma_destino", "motor", "modelo", "traducao", "criada_em"]


class TransferError(Exception):
    """Arquivo que não é uma exportação do MangaOverlay, ou de uma versão mais nova do formato."""


# --- exportar ------------------------------------------------------------------------------


def export_data(
    db: Database, path: str | Path, work_ids: list[int], loose: bool = False, parts: tuple[str, ...] = tuple(PARTS)
) -> dict[str, int]:
    """Grava as `parts` (ver PARTS) das obras `work_ids` em `path`; com `loose` e "traducoes", também as traduções
    sem obra. Retorna as contagens."""
    parts = tuple(p for p in PARTS if p in parts)
    if not parts:
        raise ValueError("Escolha ao menos uma parte para exportar.")
    with db.locked() as conn:
        works = [_export_work(conn, work_id, parts) for work_id in work_ids]
        loose_rows = _translations(conn, None) if loose and "traducoes" in parts else []
    data = {
        "formato": FORMAT,
        "versao": VERSION,
        "app": __version__,
        "exportado_em": datetime.now().isoformat(timespec="seconds"),
        "partes": list(parts),
        "campos_traducao": _TRANSLATION_FIELDS,
        "obras": [w for w in works if w is not None],
        "traducoes_sem_obra": loose_rows,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr(_DATA_FILE, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(path)
    return _count(data)


def _export_work(conn, work_id: int, parts: tuple[str, ...]) -> dict | None:
    row = conn.execute("SELECT nome, idioma_origem, criada_em FROM obras WHERE id = ?", (work_id,)).fetchone()
    if row is None:
        return None
    name, source_lang, created = row
    work = {"nome": name, "idioma_origem": source_lang}
    if "obras" in parts:
        work["criada_em"] = created
        work.update(_work_details(conn, work_id))
    if "paginas" in parts:
        work["capitulos"] = _chapters(conn, work_id)
    if "traducoes" in parts:
        work["traducoes"] = _translations(conn, work_id)
    return work


def _chapters(conn, work_id: int) -> list[dict]:
    chapters = []
    for chapter_id, *chapter in conn.execute(
        """SELECT id, nome, ordem, origem, criado_em, nomes_levantados, resumido FROM capitulos
           WHERE obra_id = ? ORDER BY ordem, nome""",
        (work_id,),
    ).fetchall():
        pages = []
        for page_id, number, file, state, error in conn.execute(
            "SELECT id, numero, arquivo, estado, erro FROM paginas WHERE capitulo_id = ? ORDER BY numero", (chapter_id,)
        ).fetchall():
            regions = conn.execute(
                "SELECT x0, y0, x1, y1, texto_original FROM regioes WHERE pagina_id = ? ORDER BY ordem", (page_id,)
            ).fetchall()
            pages.append({"numero": number, "arquivo": file, "estado": state, "erro": error, "regioes": [list(r) for r in regions]})
        chapter_name, order, origin, chapter_created, names_done, summarized = chapter
        chapters.append({
            "nome": chapter_name, "ordem": order, "origem": origin, "criado_em": chapter_created,
            "nomes_levantados": bool(names_done), "resumido": bool(summarized), "paginas": pages,
        })
    return chapters


def _work_details(conn, work_id: int) -> dict:
    characters = [
        dict(zip(["nome", "nome_original", "genero", "jeito_de_falar", "notas"], r))
        for r in conn.execute(
            "SELECT nome, nome_original, genero, jeito_de_falar, notas FROM personagens WHERE obra_id = ? ORDER BY nome",
            (work_id,),
        ).fetchall()
    ]
    memory = conn.execute("SELECT resumo, atualizada_em FROM memoria WHERE obra_id = ?", (work_id,)).fetchone()
    glossary = [
        dict(zip(["original", "traducao", "nota", "vezes", "ativo"], r))
        for r in conn.execute(
            "SELECT original, traducao, nota, vezes, ativo FROM glossario WHERE obra_id = ? ORDER BY original", (work_id,)
        ).fetchall()
    ]
    return {
        "personagens": characters,
        "memoria": {"resumo": memory[0] if memory else "", "atualizada_em": memory[1] if memory else None, "glossario": glossary},
    }


def _translations(conn, work_id: int | None) -> list[list]:
    where = "obra_id = ?" if work_id is not None else "obra_id IS NULL"
    params = (work_id,) if work_id is not None else ()
    return [
        list(r)
        for r in conn.execute(
            f"""SELECT texto_original, idioma_origem, idioma_destino, motor, modelo, traducao, criada_em FROM traducoes
                WHERE {where} ORDER BY id""",
            params,
        ).fetchall()
    ]


# --- ler e conferir ------------------------------------------------------------------------


def read_data(path: str | Path) -> dict:
    try:
        with zipfile.ZipFile(path) as archive:
            data = json.loads(archive.read(_DATA_FILE).decode("utf-8"))
    except (OSError, KeyError, zipfile.BadZipFile, ValueError) as exc:
        raise TransferError(f"Não é uma exportação do MangaOverlay: {exc}") from exc
    if not isinstance(data, dict) or data.get("formato") != FORMAT:
        raise TransferError("Não é uma exportação do MangaOverlay.")
    if not isinstance(data.get("versao"), int) or data["versao"] > VERSION:
        raise TransferError(
            f"Exportação feita por uma versão mais nova do MangaOverlay (formato {data.get('versao')}). Atualize o app para importar."
        )
    return data


@dataclass
class WorkPreview:
    name: str
    exists: bool
    chapters: int
    new_chapters: int
    pages: int
    translations: int
    characters: int
    terms: int


@dataclass
class Preview:
    exported_at: str
    parts: list[str] = field(default_factory=lambda: list(PARTS))
    works: list[WorkPreview] = field(default_factory=list)
    loose_translations: int = 0


def preview(db: Database, data: dict) -> Preview:
    """O que a importação traria: por obra, se ela já existe e quantos capítulos são novos (os outros são pulados)."""
    result = Preview(
        exported_at=str(data.get("exportado_em", "")),
        parts=parts_of(data),
        loose_translations=len(data.get("traducoes_sem_obra", [])),
    )
    with db.locked() as conn:
        for work in data.get("obras", []):
            row = conn.execute("SELECT id FROM obras WHERE nome = ? COLLATE NOCASE", (work["nome"],)).fetchone()
            origins = set()
            if row:
                origins = {o for (o,) in conn.execute("SELECT origem FROM capitulos WHERE obra_id = ?", (row[0],))}
            chapters = work.get("capitulos", [])
            result.works.append(WorkPreview(
                name=work["nome"],
                exists=row is not None,
                chapters=len(chapters),
                new_chapters=sum(1 for c in chapters if c["origem"] not in origins),
                pages=sum(len(c.get("paginas", [])) for c in chapters),
                translations=len(work.get("traducoes", [])),
                characters=len(work.get("personagens", [])),
                terms=len(work.get("memoria", {}).get("glossario", [])),
            ))
    return result


# --- importar ------------------------------------------------------------------------------


@dataclass
class ImportResult:
    works_created: int = 0
    works_merged: int = 0
    chapters: int = 0
    chapters_skipped: int = 0
    pages: int = 0
    translations: int = 0
    translations_skipped: int = 0
    characters: int = 0
    terms: int = 0


def import_data(db: Database, data: dict) -> ImportResult:
    """Mescla `data` (de read_data) no banco, sem sobrescrever nada. Tudo ou nada: um erro desfaz a importação."""
    result = ImportResult()
    with db.locked() as conn:
        conn.execute("BEGIN")
        try:
            for work in data.get("obras", []):
                _import_work(conn, work, result)
            _import_translations(conn, None, data.get("traducoes_sem_obra", []), result)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return result


def _import_work(conn, work: dict, result: ImportResult) -> None:
    row = conn.execute("SELECT id FROM obras WHERE nome = ? COLLATE NOCASE", (work["nome"],)).fetchone()
    if row:
        work_id = row[0]
        result.works_merged += 1
    else:
        work_id = conn.execute(
            "INSERT INTO obras (nome, idioma_origem, criada_em) VALUES (?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
            (work["nome"], work["idioma_origem"], work.get("criada_em")),
        ).lastrowid
        result.works_created += 1

    for chapter in work.get("capitulos", []):
        exists = conn.execute("SELECT 1 FROM capitulos WHERE obra_id = ? AND origem = ?", (work_id, chapter["origem"])).fetchone()
        if exists:
            result.chapters_skipped += 1
            continue
        chapter_id = conn.execute(
            """INSERT INTO capitulos (obra_id, nome, ordem, origem, criado_em, nomes_levantados, resumido)
               VALUES (?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?)""",
            (work_id, chapter["nome"], chapter["ordem"], chapter["origem"], chapter.get("criado_em"),
             int(chapter.get("nomes_levantados", False)), int(chapter.get("resumido", False))),
        ).lastrowid
        result.chapters += 1
        for page in chapter.get("paginas", []):
            page_id = conn.execute(
                "INSERT INTO paginas (capitulo_id, numero, arquivo, estado, erro) VALUES (?, ?, ?, ?, ?)",
                (chapter_id, page["numero"], page["arquivo"], page.get("estado", "lida"), page.get("erro")),
            ).lastrowid
            conn.executemany(
                "INSERT INTO regioes (pagina_id, ordem, x0, y0, x1, y1, texto_original) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(page_id, order, *region) for order, region in enumerate(page.get("regioes", []), start=1)],  # como save_page_texts
            )
            result.pages += 1

    for character in work.get("personagens", []):
        cursor = conn.execute(
            """INSERT OR IGNORE INTO personagens (obra_id, nome, nome_original, genero, jeito_de_falar, notas)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (work_id, character["nome"], character.get("nome_original", ""), character.get("genero", ""),
             character.get("jeito_de_falar", ""), character.get("notas", "")),
        )
        result.characters += cursor.rowcount

    memory = work.get("memoria") or {}
    if memory.get("resumo"):
        # O resumo existente vence: só entra o importado se a obra ainda não tiver um
        conn.execute("INSERT OR IGNORE INTO memoria (obra_id, resumo, atualizada_em) VALUES (?, '', NULL)", (work_id,))
        conn.execute(
            "UPDATE memoria SET resumo = ?, atualizada_em = ? WHERE obra_id = ? AND resumo = ''",
            (memory["resumo"], memory.get("atualizada_em"), work_id),
        )
    for term in memory.get("glossario", []):
        cursor = conn.execute(
            "INSERT OR IGNORE INTO glossario (obra_id, original, traducao, nota, vezes, ativo) VALUES (?, ?, ?, ?, ?, ?)",
            (work_id, term["original"], term["traducao"], term.get("nota", ""), term.get("vezes", 1), int(term.get("ativo", 0))),
        )
        result.terms += cursor.rowcount

    _import_translations(conn, work_id, work.get("traducoes", []), result)


def _import_translations(conn, work_id: int | None, rows: list[list], result: ImportResult) -> None:
    for row in rows:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO traducoes
               (obra_id, texto_original, idioma_origem, idioma_destino, motor, modelo, traducao, criada_em)
               VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))""",
            (work_id, *row[:7]) if len(row) >= 7 else (work_id, *row[:6], None),
        )
        if cursor.rowcount:
            result.translations += 1
        else:
            result.translations_skipped += 1


def parts_of(data: dict) -> list[str]:
    """Partes presentes no arquivo (sem a lista, o arquivo tem todas)."""
    return [p for p in data.get("partes", PARTS) if p in PARTS]


def _count(data: dict) -> dict[str, int]:
    works = data["obras"]
    return {
        "obras": len(works),
        "capitulos": sum(len(w.get("capitulos", [])) for w in works),
        "paginas": sum(len(c["paginas"]) for w in works for c in w.get("capitulos", [])),
        "traducoes": sum(len(w.get("traducoes", [])) for w in works) + len(data["traducoes_sem_obra"]),
    }


def run_cli(export_path: str | None, work_names: list[str] | None, import_path: str | None, parts: str | None = None) -> int:
    """`--export ARQUIVO [--obra NOME]... [--partes obras,paginas,traducoes]` e `--import ARQUIVO`."""
    import sys

    db = Database()
    try:
        if export_path:
            works = db.works()
            if work_names:
                wanted = {n.casefold() for n in work_names}
                missing = wanted - {w.name.casefold() for w in works}
                if missing:
                    print(f"Obra(s) não encontrada(s): {', '.join(sorted(missing))}", file=sys.stderr)
                    return 2
                works = [w for w in works if w.name.casefold() in wanted]
            chosen = tuple(p.strip() for p in parts.split(",")) if parts else tuple(PARTS)
            unknown = [p for p in chosen if p not in PARTS]
            if unknown or not chosen:
                print(f"Partes desconhecidas: {', '.join(unknown)}. Use: {', '.join(PARTS)}", file=sys.stderr)
                return 2
            counts = export_data(db, export_path, [w.id for w in works], loose=not work_names, parts=chosen)
            shown = {"obras"} | ({"capitulos", "paginas"} if "paginas" in chosen else set()) | ({"traducoes"} if "traducoes" in chosen else set())
            print(f"Exportado para {export_path}: " + ", ".join(f"{v} {k}" for k, v in counts.items() if k in shown), file=sys.stderr)
        if import_path:
            try:
                data = read_data(import_path)
            except TransferError as exc:
                print(exc, file=sys.stderr)
                return 1
            result = import_data(db, data)
            print(
                f"Importado: {result.works_created} obra(s) nova(s), {result.works_merged} mesclada(s), "
                f"{result.chapters} capítulo(s) ({result.chapters_skipped} já existiam), {result.pages} página(s), "
                f"{result.translations} tradução(ões) ({result.translations_skipped} já existiam), "
                f"{result.characters} personagem(ns), {result.terms} termo(s)",
                file=sys.stderr,
            )
    finally:
        db.close()
    return 0
