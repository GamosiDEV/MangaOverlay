"""Tradução em lote dos textos de uma tela.

Os motores com LLM ficam em llm_openai.py e llm_anthropic.py; aqui estão o Google gratuito,
o NLLB offline e o que é comum aos LLMs (instruções e formato das respostas).
"""

import io
import json
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from .db import Character, Memory, Term
from .languages import AUTO, SOURCES, TARGETS, source_english, target_english


class TranslationError(Exception):
    """Falha ao traduzir. `retryable`: erro temporário (limite de requisições, servidor, rede) que vale tentar de novo."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class MalformedResponse(TranslationError):
    """O modelo respondeu, mas fora do formato esperado (vale pedir de novo)."""


@dataclass
class Usage:
    """Tokens cobrados pela API numa chamada (zero nos motores locais/gratuitos)."""

    input_tokens: int = 0
    cached_tokens: int = 0  # parte da entrada lida do cache de prompt (mais barata)
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.cached_tokens + other.cached_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass
class Line:
    id: int
    text: str  # vazio no modo visão (o LLM lê a imagem)
    where: str = ""  # "Cap 3, p. 12" na tradução em lote: marca a mudança de cena


@dataclass
class Result:
    id: int
    original: str
    translation: str


# --- Google Tradutor gratuito -------------------------------------------------------

_GOOGLE_CODES = {"zh-CN": "zh-CN", "zh-TW": "zh-TW", AUTO: "auto"}


def translate_google(lines: list[Line], source: str, target: str) -> list[Result]:
    from deep_translator import GoogleTranslator
    from deep_translator.exceptions import TooManyRequests

    translator = GoogleTranslator(source=_GOOGLE_CODES.get(source, source), target=target)
    texts = [line.text.replace("\n", " ") for line in lines]
    try:
        # Uma requisição só para a tela inteira; se o Google juntar ou quebrar linhas, traduz uma a uma.
        joined = translator.translate("\n".join(texts)) or ""
        parts = joined.split("\n")
        if len(parts) != len(texts):
            parts = [translator.translate(text) or "" for text in texts]
    except TooManyRequests as exc:
        raise TranslationError(
            "O Google gratuito bloqueou temporariamente as requisições da sua rede (erro 429). "
            "Tente mais tarde ou troque o motor no menu da bandeja.",
            retryable=True,
        ) from exc
    except Exception as exc:  # deep-translator repassa erros próprios e de rede/HTTP variados
        raise TranslationError(f"Erro no Google Tradutor: {exc}") from exc
    return [Result(line.id, line.text, part.strip()) for line, part in zip(lines, parts)]


# --- NLLB (offline, na GPU) -----------------------------------------------------------

NLLB_ID = "facebook/nllb-200-distilled-600M"  # licença CC-BY-NC 4.0: uso pessoal/não comercial


class NllbTranslator:
    def __init__(self, device: str):
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        self._torch = torch
        self.device = device
        self.tokenizer = AutoTokenizer.from_pretrained(NLLB_ID)
        dtype = torch.float16 if device.startswith("cuda") else torch.float32
        self.model = AutoModelForSeq2SeqLM.from_pretrained(NLLB_ID, dtype=dtype).to(device).eval()

    def translate(self, lines: list[Line], source: str, target: str) -> list[Result]:
        if source not in SOURCES:
            raise TranslationError("A tradução offline precisa do idioma de origem definido (não funciona com 'Detectar').")
        self.tokenizer.src_lang = SOURCES[source][2]
        inputs = self.tokenizer([line.text for line in lines], return_tensors="pt", padding=True).to(self.device)
        target_token = self.tokenizer.convert_tokens_to_ids(TARGETS[target][2])
        with self._torch.inference_mode():
            tokens = self.model.generate(**inputs, forced_bos_token_id=target_token, max_new_tokens=256)
        outputs = self.tokenizer.batch_decode(tokens, skip_special_tokens=True)
        return [Result(line.id, line.text, text.strip()) for line, text in zip(lines, outputs)]


# --- Comum aos LLMs -------------------------------------------------------------------


def _source_clause(source: str) -> str:
    if source == AUTO:
        return "The source language may be Japanese, Korean, Chinese or English; detect it"
    return (
        f"The source language is {source_english(source)}; a few texts may be in another language (for example "
        "Japanese left in the artwork of a translated edition): translate those too"
    )


def characters_block(characters: list[Character]) -> str:
    """Lista de personagens da obra, no formato que vai para o modelo (vazio se não houver)."""
    if not characters:
        return ""
    lines = []
    for c in characters:
        details = [d for d in (c.gender, c.speech, c.notes) if d]
        original = f"{c.original} → " if c.original else ""
        lines.append(f"- {original}{c.name}" + (f" ({'; '.join(details)})" if details else ""))
    return (
        "\n\nCharacters of this series (original name → name to use in the translation; gender and way of speaking "
        "in Portuguese when given). Always write these names exactly as listed and respect gender and speech style:\n"
        + "\n".join(lines)
    )


def memory_block(memory: Memory | None) -> str:
    """Memória da obra (resumo e glossário) no formato que vai para o modelo. Ordem fixa: favorece o cache de prompt."""
    if memory is None or memory.empty:
        return ""
    parts = []
    if memory.summary:
        parts.append("Story so far (summary of the previous chapters, for context only):\n" + memory.summary)
    if memory.glossary:
        terms = "\n".join(f"- {t.original} → {t.translation}" + (f" ({t.note})" if t.note else "") for t in memory.glossary)
        parts.append("Glossary of this series (always translate these terms exactly like this):\n" + terms)
    return "\n\n" + "\n\n".join(parts)


_NEW_TERMS = (
    " In `new_terms`, list proper nouns and series-specific terms from these texts (places, groups, techniques, "
    "titles, catchphrases) that are not yet in the character list or glossary above, with the translation you used "
    "and, in Portuguese, a short note on what it is; usually it is empty."
)


def llm_instructions(
    source: str, target: str, vision: bool, characters: list[Character] | None = None, memory: Memory | None = None
) -> str:
    task = (
        "The user sends a screenshot of a comic page. Each text to translate is marked with a red box and its "
        "number. For every numbered box, transcribe the original text inside it and translate it."
        if vision
        else "The user sends a JSON object with the numbered texts of a comic page, read by OCR "
        "(which may contain small reading errors: infer the intended text)."
    )
    return (
        "You translate manga, manhwa, manhua and comics for a reader who does not know the original language. "
        f"{task} {_source_clause(source)}. Translate into natural, colloquial {target_english(target)}, as a "
        "professional scanlation would: keep each character's tone, keep honorifics such as -san and -kun, and "
        "turn sound effects into equivalent onomatopoeia. Keep each translation short, because it has to fit in "
        "the original speech bubble. The texts are in reading order and belong to the same scene; "
        "the optional context holds the lines of the previous pages. "
        "Return one item per number, with an empty translation if a box has no readable text." + _NEW_TERMS
    ) + characters_block(characters or []) + memory_block(memory)


def response_schema(vision: bool) -> dict:
    properties = {"id": {"type": "integer"}, "translation": {"type": "string"}}
    if vision:
        properties["original"] = {"type": "string"}
    term = {"original": {"type": "string"}, "translation": {"type": "string"}, "note": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                    "additionalProperties": False,
                },
            },
            "new_terms": {
                "type": "array",
                "items": {"type": "object", "properties": term, "required": list(term), "additionalProperties": False},
            },
        },
        "required": ["items", "new_terms"],
        "additionalProperties": False,
    }


def parse_terms(raw: str) -> list[Term]:
    """Termos novos que o modelo informou (lista vazia se a resposta não trouxer)."""
    try:
        items = json.loads(raw).get("new_terms") or []
    except (ValueError, AttributeError):
        return []
    return [
        Term(str(t.get("original", "")), str(t.get("translation", "")), str(t.get("note", "")))
        for t in items
        if isinstance(t, dict)
    ]


def text_request(lines: list[Line], context: list[str]) -> str:
    texts = [{"id": line.id, "page": line.where, "text": line.text} if line.where else {"id": line.id, "text": line.text} for line in lines]
    payload = {"context": context, "texts": texts}
    return json.dumps(payload, ensure_ascii=False)


def parse_response(raw: str, lines: list[Line]) -> list[Result]:
    try:
        items = json.loads(raw)["items"]
    except (ValueError, KeyError, TypeError) as exc:
        raise MalformedResponse(f"Resposta inesperada do modelo: {raw[:200]}") from exc
    by_id = {line.id: line for line in lines}
    results = []
    for item in items:
        line = by_id.get(item.get("id"))
        if line is None:
            continue
        original = item.get("original") or line.text
        results.append(Result(line.id, str(original), str(item.get("translation", "")).strip()))
    return results


def marked_page(image: Image.Image, boxes: dict[int, tuple[int, int, int, int]], max_side: int = 1800) -> bytes:
    """Imagem JPEG da tela com uma caixa vermelha numerada em cada texto (para os LLMs com visão)."""
    scale = min(1.0, max_side / max(image.size))
    page = image.convert("RGB")
    if scale < 1:
        page = page.resize((round(page.width * scale), round(page.height * scale)), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=max(14, round(18 * scale)))
    for number, (x0, y0, x1, y1) in boxes.items():
        box = [round(v * scale) for v in (x0, y0, x1, y1)]
        draw.rectangle(box, outline=(230, 0, 0), width=3)
        label = str(number)
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
        lx, ly = max(0, box[0] - (right - left) - 6), max(0, box[1])
        draw.rectangle([lx, ly, lx + (right - left) + 6, ly + (bottom - top) + 6], fill=(230, 0, 0))
        draw.text((lx + 3 - left, ly + 3 - top), label, fill="white", font=font)
    buffer = io.BytesIO()
    page.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()
