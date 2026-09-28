"""Capítulos a importar: pastas de imagens, arquivos CBZ/ZIP e PDFs.

Uma pasta com imagens é um capítulo. Uma pasta com subpastas, CBZs ou PDFs vira vários capítulos
(é assim que se importam 30 capítulos de uma vez). As imagens nunca são copiadas: o banco guarda
a origem e o nome de cada página, e `load_page` abre direto do original.
"""

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
ARCHIVE_EXTENSIONS = {".cbz", ".zip"}
PDF_EXTENSIONS = {".pdf"}
# PDFs não têm "resolução em pixels": renderiza com o lado maior neste tamanho
PDF_LONG_SIDE = 2000

_CHAPTER_WORDS = re.compile(r"(?:cap[ií]tulo|chapter|chap|cap|ch|ep|episode|第|#)\s*[._-]?\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class ChapterSource:
    name: str
    origin: str  # caminho absoluto da pasta ou do arquivo
    order: float  # número do capítulo extraído do nome (para ordenar)
    files: list[str]  # páginas, na ordem: nomes de arquivo (pasta/CBZ) ou índices (PDF)


def natural_key(text: str) -> list:
    """"pag2" antes de "pag10"."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def chapter_number(name: str) -> float | None:
    """Número do capítulo no nome ("Cap. 12", "ch012", "第3話", "Vol 2 - 15"). Prefere o número após a palavra capítulo."""
    match = _CHAPTER_WORDS.search(name)
    numbers = [match.group(1)] if match else _NUMBER.findall(name)
    if not numbers:
        return None
    return float(numbers[-1].replace(",", "."))


def _is_image(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTENSIONS and not Path(name).name.startswith(".")


def _archive_pages(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if _is_image(n) and not n.startswith("__MACOSX/")]
    return sorted(names, key=natural_key)


def _pdf_pages(path: Path) -> list[str]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        return [str(index) for index in range(len(document))]
    finally:
        document.close()


def _folder_pages(path: Path) -> list[str]:
    return sorted((p.name for p in path.iterdir() if p.is_file() and _is_image(p.name)), key=natural_key)


def _chapter(path: Path, files: list[str]) -> ChapterSource | None:
    if not files:
        return None
    name = path.stem if path.is_file() else path.name
    number = chapter_number(name)
    return ChapterSource(name=name, origin=str(path.resolve()), order=number if number is not None else 0.0, files=files)


def discover(paths: list[Path]) -> tuple[list[ChapterSource], list[str]]:
    """Capítulos encontrados nos caminhos escolhidos (ordenados) e avisos sobre o que foi ignorado."""
    chapters: list[ChapterSource] = []
    warnings: list[str] = []

    def visit(path: Path) -> None:
        suffix = path.suffix.lower()
        try:
            if path.is_dir():
                chapter = _chapter(path, _folder_pages(path))
                if chapter:
                    chapters.append(chapter)
                for child in sorted(path.iterdir(), key=lambda p: natural_key(p.name)):
                    if child.is_dir() or child.suffix.lower() in ARCHIVE_EXTENSIONS | PDF_EXTENSIONS:
                        visit(child)
            elif suffix in ARCHIVE_EXTENSIONS:
                chapter = _chapter(path, _archive_pages(path))
                if chapter:
                    chapters.append(chapter)
                else:
                    warnings.append(f"{path.name}: nenhuma imagem dentro do arquivo")
            elif suffix in PDF_EXTENSIONS:
                chapter = _chapter(path, _pdf_pages(path))
                if chapter:
                    chapters.append(chapter)
            elif _is_image(path.name):
                # Imagens soltas escolhidas uma a uma: a pasta delas vira o capítulo
                visit(path.parent)
            else:
                warnings.append(f"{path.name}: formato não suportado (use pasta de imagens, CBZ, ZIP ou PDF)")
        # Arquivo corrompido, sem permissão, ZIP inválido, PDF com senha (o pypdfium2 tem erros próprios)
        except Exception as exc:
            warnings.append(f"{path.name}: não foi possível abrir ({exc})")

    for path in paths:
        visit(Path(path))

    # Sem duplicatas (ex.: várias imagens da mesma pasta escolhidas), ordenados pelo número do capítulo
    unique = {c.origin: c for c in chapters}
    ordered = sorted(unique.values(), key=lambda c: (c.order, natural_key(c.name)))
    return ordered, warnings


def load_page(origin: str, file: str) -> Image.Image:
    """Abre uma página a partir da origem registrada no banco."""
    path = Path(origin)
    suffix = path.suffix.lower()
    if path.is_dir():
        with Image.open(path / file) as image:
            return image.convert("RGB")
    if suffix in ARCHIVE_EXTENSIONS:
        with zipfile.ZipFile(path) as archive, archive.open(file) as handle, Image.open(handle) as image:
            return image.convert("RGB")
    if suffix in PDF_EXTENSIONS:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(path))
        try:
            page = document[int(file)]
            width, height = page.get_size()
            return page.render(scale=PDF_LONG_SIDE / max(width, height)).to_pil().convert("RGB")
        finally:
            document.close()
    raise OSError(f"Origem desconhecida: {origin}")
