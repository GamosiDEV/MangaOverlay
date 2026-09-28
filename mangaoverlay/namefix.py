"""Revisão de nomes nas traduções já salvas de uma obra.

Três fontes de correção, todas mostradas ao usuário antes de gravar:
- renomeação: o nome de um personagem mudou na lista → troca o nome antigo pelo novo (grátis);
- variação: o original da fala cita o personagem (春斗) e a tradução traz um nome parecido e errado
  ("Harutou-kun") → troca pelo nome da lista, mantendo o tratamento (grátis);
- IA: o original cita o personagem, mas nenhum nome parecido aparece na tradução → o modelo revê
  só essas falas (custo mostrado antes).
"""

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from . import credentials, llm_anthropic, llm_openai, pricing
from .config import Config
from .db import Character, Database
from .names import _provider
from .translators import TranslationError, Usage, characters_block

_WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ']+")
# Palavras comuns do português parecidas com nomes curtos (Mina x Minha, Ana x Nada…): nunca são trocadas
_COMMON = {
    "minha", "minhas", "mina", "nada", "nana", "sua", "suas", "seu", "seus", "meu", "meus", "tua", "teu", "mais",
    "mas", "mano", "mana", "hoje", "hora", "horas", "haru", "sim", "não", "nao", "então", "entao", "ainda", "aqui",
    "ali", "lá", "cara", "caro", "casa", "rua", "vai", "vamos", "sei", "sabe", "tudo", "todo", "toda", "muito",
    "muita", "pouco", "tanto", "quanto", "quando", "onde", "como", "porque", "pra", "para", "pela", "pelo", "com",
    "sem", "sob", "sobre", "ela", "ele", "elas", "eles", "você", "voce", "vocês", "nós", "nos", "isso", "isto",
    "aquilo", "esse", "essa", "este", "esta", "aquele", "aquela", "sério", "serio", "certo", "certa", "bom", "boa",
    "mal", "mau", "ruim", "senhor", "senhora", "moça", "moço", "garoto", "garota", "menino", "menina", "irmão",
    "irmã", "mãe", "pai", "tia", "tio", "professor", "professora", "amigo", "amiga", "espera", "calma", "olha",
}
_AI_CHUNK = 80  # falas por pedido na revisão com IA


@dataclass
class Change:
    translation_id: int
    original: str
    before: str
    after: str
    reason: str  # "renomeado", "variação" ou "IA"


def _replace_word(text: str, old: str, new: str) -> str:
    """Troca `old` por `new` como palavra inteira (mantém "-kun", "!", pontuação ao redor)."""
    return re.sub(rf"(?<![A-Za-zÀ-ÖØ-öø-ÿ']){re.escape(old)}(?![A-Za-zÀ-ÖØ-öø-ÿ'])", new, text)


def rename_changes(db: Database, work_id: int, renames: list[tuple[str, str]]) -> list[Change]:
    """Troca de grafia feita na lista de personagens: nome antigo → novo em todas as traduções da obra."""
    changes: dict[int, Change] = {}
    for translation_id, original, text in db.work_translations(work_id):
        after = text
        for old, new in renames:
            if old and new and old != new:
                after = _replace_word(after, old, new)
        if after != text:
            changes[translation_id] = Change(translation_id, original, text, after, "renomeado")
    return list(changes.values())


def _similar_word(text: str, name: str) -> str | None:
    """Palavra da tradução que parece uma grafia errada do nome (ex.: "Harutou" para "Haruto")."""
    best, best_ratio = None, 0.72
    for word in _WORD.findall(text):
        if word == name or word.casefold() in _COMMON or not word[0].isupper():
            continue
        if word[0].casefold() != name[0].casefold() or abs(len(word) - len(name)) > 2:
            continue
        ratio = SequenceMatcher(None, word.casefold(), name.casefold()).ratio()
        if ratio >= best_ratio:
            best, best_ratio = word, ratio
    return best


@dataclass
class Scan:
    changes: list[Change]  # variações corrigidas localmente (grátis)
    ambiguous: list[tuple[int, str, str]]  # (id, original, tradução): cita personagem, mas o nome não aparece


def scan_variations(db: Database, work_id: int, characters: list[Character]) -> Scan:
    """Procura, nas falas cujo original cita um personagem, a grafia errada do nome na tradução."""
    named = [c for c in characters if c.original and c.name]
    changes, ambiguous = [], []
    for translation_id, original, text in db.work_translations(work_id):
        cited = [c for c in named if c.original in original]
        if not cited:
            continue
        after, missing = text, False
        for character in cited:
            # Palavra inteira: "Haruto" não conta como presente dentro de "Harutou"
            if re.search(rf"(?<![A-Za-zÀ-ÖØ-öø-ÿ']){re.escape(character.name)}(?![A-Za-zÀ-ÖØ-öø-ÿ'])", after):
                continue
            wrong = _similar_word(after, character.name)
            if wrong:
                after = _replace_word(after, wrong, character.name)
            else:
                missing = True
        if after != text:
            changes.append(Change(translation_id, original, text, after, "variação"))
        elif missing:
            ambiguous.append((translation_id, original, text))
    return Scan(changes, ambiguous)


def all_citing_lines(db: Database, work_id: int, characters: list[Character]) -> list[tuple[int, str, str]]:
    """Revisão da obra inteira: todas as falas cujo original cita algum personagem da lista."""
    originals = [c.original for c in characters if c.original]
    return [row for row in db.work_translations(work_id) if any(o in row[1] for o in originals)]


# --- revisão com IA --------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "translation": {"type": "string"}},
                "required": ["id", "translation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _instructions(characters: list[Character]) -> str:
    return (
        "You review names in a Brazilian Portuguese translation of a comic. For each item you get the original line "
        "and its current translation. Fix ONLY character names: when the original mentions a character, the "
        "translation must use exactly the name from the list below (keeping honorifics such as -kun or -san as in the "
        "original) and the right gender agreement for that character. Do not change anything else; if the line is "
        "already right, return it unchanged." + characters_block(characters)
    )


@dataclass
class ReviewPlan:
    provider: str
    model: str
    lines: list[tuple[int, str, str]]
    estimated_cost: float | None


def plan_review(config: Config, lines: list[tuple[int, str, str]]) -> ReviewPlan:
    provider = _provider(config)
    model = config.openai_model if provider == "openai" else config.claude_model
    input_tokens = sum(pricing.estimate_tokens(o + t) + 15 for _i, o, t in lines) + 800 * max(1, -(-len(lines) // _AI_CHUNK))
    output_tokens = sum(pricing.estimate_tokens(t) + 10 for _i, _o, t in lines)
    return ReviewPlan(provider, model, lines, pricing.cost(model, input_tokens, output_tokens))


def run_review(plan: ReviewPlan, characters: list[Character]) -> tuple[list[Change], float]:
    """Envia as falas em pedidos de até 80 e devolve só as que o modelo alterou, com o custo real."""
    changes, cost, usage_total = [], 0.0, Usage()
    by_id = {i: (o, t) for i, o, t in plan.lines}
    for start in range(0, len(plan.lines), _AI_CHUNK):
        chunk = plan.lines[start : start + _AI_CHUNK]
        payload = json.dumps({"items": [{"id": i, "original": o, "translation": t} for i, o, t in chunk]}, ensure_ascii=False)
        if plan.provider == "openai":
            raw, usage = llm_openai.structured(credentials.get_key(credentials.OPENAI), plan.model, _instructions(characters), payload, _SCHEMA, "review")
        else:
            raw, usage = llm_anthropic.structured(
                credentials.get_key(credentials.ANTHROPIC), plan.model, _instructions(characters), [{"type": "text", "text": payload}], _SCHEMA
            )
        usage_total = usage_total + usage
        cost += pricing.cost(plan.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens) or 0.0
        try:
            items = json.loads(raw)["items"]
        except (ValueError, KeyError, TypeError) as exc:
            raise TranslationError(f"Resposta inesperada do modelo: {raw[:200]}") from exc
        for item in items:
            translation_id = item.get("id")
            if translation_id not in by_id:
                continue
            original, before = by_id[translation_id]
            after = str(item.get("translation", "")).strip()
            if after and after != before:
                changes.append(Change(translation_id, original, before, after, "IA"))
    return changes, cost

