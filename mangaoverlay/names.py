"""Nomes de personagens: sugestões locais (grátis) e levantamento com IA (rápido ou completo).

Sugestões locais: em japonês e coreano, nomes costumam vir seguidos de um tratamento (田中さん, 美咲ちゃん,
지수 씨) ou, em japonês, escritos em katakana. Contamos essas ocorrências no texto já lido pelo OCR.

Levantamento com IA: o modelo lê o texto dos capítulos importados e devolve os personagens com o nome
sugerido na tradução, gênero e jeito de falar. O resultado sempre passa pela revisão do usuário na
tela da lista antes de ser usado.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass

from . import credentials, llm_anthropic, llm_openai, pricing
from .config import Config
from .db import ChapterTexts, Character, Database
from .languages import source_english, target_english

# Tratamentos que costumam vir logo depois de um nome
_JA_HONORIFICS = "さん|くん|君|ちゃん|様|さま|先輩|せんぱい|先生|殿|氏|たん|っち"
_JA_NAME = re.compile(rf"([一-鿿぀-ゟ゠-ヿ]{{1,6}}?)(?:{_JA_HONORIFICS})")
_JA_KATAKANA = re.compile(r"[゠-ヿ]{2,}")
_KO_HONORIFICS = "씨|님|선배|오빠|언니|누나|형|선생님"
_TRAILING_HONORIFIC = re.compile(rf"\s*(?:{_JA_HONORIFICS}|{_KO_HONORIFICS})$")
_KO_NAME = re.compile(r"([가-힯]{2,4})\s?(?:씨|님|선배|오빠|언니|누나|형|선생님)")
# Palavras em katakana muito comuns que não são nomes
_KATAKANA_STOPWORDS = {"ー", "ッ", "ドキ", "ドン", "バン", "ゴゴゴ", "ザワ", "ハハ", "フフ", "ニャ", "オイ", "ハイ", "エッ", "アッ"}
# Pronomes/palavras comuns antes de tratamentos
_JA_STOPWORDS = {"お", "み", "皆", "みな", "みんな", "あなた", "おじ", "おば", "お兄", "お姉", "おにい", "おねえ", "お父", "お母"}

# Estimativa de saída por pedaço de texto (lista de personagens em JSON)
_OUTPUT_TOKENS_PER_CHUNK = 1500
# Tamanho de cada pedido do levantamento (tokens de entrada, aproximado)
_CHUNK_TOKENS = 30_000


class SurveyError(Exception):
    pass


def suggest_names(texts: list[str], source: str, limit: int = 40) -> list[tuple[str, int]]:
    """Possíveis nomes no texto, com o número de ocorrências (mais frequentes primeiro)."""
    counts: Counter[str] = Counter()
    for text in texts:
        if source == "ja":
            for match in _JA_NAME.finditer(text):
                name = match.group(1)
                if name not in _JA_STOPWORDS and not re.fullmatch(r"[぀-ゟ]", name):
                    counts[name] += 1
            for word in _JA_KATAKANA.findall(text):
                word = word.strip("ー・")
                if len(word) >= 2 and word not in _KATAKANA_STOPWORDS and not re.fullmatch(r"(.+?)\1+", word):  # ゴゴゴ, ドキドキ
                    counts[word] += 1
        elif source == "ko":
            counts.update(match.group(1) for match in _KO_NAME.finditer(text))
    # Katakana que aparece uma vez só costuma ser onomatopeia ou palavra estrangeira
    return [(name, count) for name, count in counts.most_common() if count >= 2 or _has_honorific(name, texts)][:limit]


def _has_honorific(name: str, texts: list[str]) -> bool:
    pattern = re.compile(re.escape(name) + rf"(?:{_JA_HONORIFICS})")
    return any(pattern.search(t) for t in texts)


# --- levantamento com IA -----------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "original": {"type": "string"},
                    "name": {"type": "string"},
                    "gender": {"type": "string", "enum": ["masculino", "feminino", "outro", ""]},
                    "speech": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["original", "name", "gender", "speech", "notes"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["characters"],
    "additionalProperties": False,
}


def _instructions(source: str, target: str, known: list[Character]) -> str:
    known_text = "\n".join(f"- {c.original or '?'} → {c.name}" for c in known) or "(none)"
    return (
        f"You help translate a comic from {source_english(source)} into {target_english(target)}. "
        "The user sends the dialogue of some chapters, read by OCR (it may contain small reading errors). "
        "List the named characters who appear or are mentioned. For each one give: `original`, the name exactly as "
        "written in the text (without honorifics such as -san or -kun); `name`, how the name should be written in the "
        f"{target_english(target)} translation (standard romanization, e.g. Hepburn for Japanese); `gender` in "
        "Portuguese (masculino, feminino, outro) or empty when the text does not make it clear; `speech`, a few words "
        "in Portuguese about how the character speaks (formal, rude, childish, archaic…), or empty; `notes`, in "
        "Portuguese, how they relate to others or which nickname they use, or empty. Only real names of characters: "
        "no generic words, sound effects or titles alone. These characters are already known; do not repeat them:\n"
        f"{known_text}"
    )


@dataclass
class SurveyPlan:
    provider: str  # "openai" ou "claude"
    model: str
    chapters: list[ChapterTexts]
    input_tokens: int
    requests: int
    estimated_cost: float | None

    @property
    def description(self) -> str:
        names = ", ".join(c.name for c in self.chapters[:3]) + ("…" if len(self.chapters) > 3 else "")
        return (
            f"{len(self.chapters)} capítulo(s) ({names}), ~{pricing.thousands(self.input_tokens)} tokens de texto"
            + f", {self.requests} pedido(s) ao modelo {self.model}. Custo estimado: {pricing.format_cost(self.estimated_cost)}."
        )


def _provider(config: Config) -> str:
    """Usa o provedor do motor atual; com motores sem LLM, o que tiver chave configurada."""
    if config.engine.startswith("claude"):
        return "claude"
    if config.engine.startswith("openai") or credentials.get_key(credentials.OPENAI):
        return "openai"
    if credentials.get_key(credentials.ANTHROPIC):
        return "claude"
    raise SurveyError(
        "O levantamento de nomes usa a IA da OpenAI ou do Claude: configure uma chave em Configurações."
    )


def _chunks(chapters: list[ChapterTexts]) -> list[list[ChapterTexts]]:
    chunks: list[list[ChapterTexts]] = [[]]
    size = 0
    for chapter in chapters:
        tokens = pricing.estimate_tokens("\n".join(chapter.texts))
        if chunks[-1] and size + tokens > _CHUNK_TOKENS:
            chunks.append([])
            size = 0
        chunks[-1].append(chapter)
        size += tokens
    return chunks


def plan_survey(db: Database, work_id: int, config: Config, full: bool) -> SurveyPlan:
    """O que o levantamento vai analisar e quanto deve custar (antes de enviar qualquer coisa)."""
    provider = _provider(config)
    chapters = db.chapter_texts(work_id, only_pending_names=True)
    if not full:
        chapters = chapters[: config.names_quick_chapters]
    if provider == "openai":
        model = config.names_model_openai if full else config.openai_model
    else:
        model = config.names_model_claude if full else config.claude_model
    input_tokens = sum(pricing.estimate_tokens("\n".join(c.texts)) for c in chapters)
    requests = len(_chunks(chapters)) if chapters else 0
    estimated = pricing.cost(model, input_tokens + requests * 600, requests * _OUTPUT_TOKENS_PER_CHUNK)
    return SurveyPlan(provider, model, chapters, input_tokens, requests, estimated)


def run_survey(db: Database, work_id: int, plan: SurveyPlan, source: str, target: str) -> list[Character]:
    """Executa o levantamento e devolve só os personagens novos. Não grava nada: a lista e a marca de
    "capítulos analisados" só são salvas quando o usuário confirma a revisão (ver CharactersDialog)."""
    known = db.characters(work_id)
    found: dict[str, Character] = {}
    for chunk in _chunks(plan.chapters):
        text = "\n\n".join(f"### {c.name}\n" + "\n".join(c.texts) for c in chunk)
        instructions = _instructions(source, target, known + list(found.values()))
        if plan.provider == "openai":
            raw, _usage = llm_openai.structured(
                credentials.get_key(credentials.OPENAI), plan.model, instructions, text, _SCHEMA, "characters"
            )
        else:
            raw, _usage = llm_anthropic.structured(
                credentials.get_key(credentials.ANTHROPIC), plan.model, instructions, [{"type": "text", "text": text}], _SCHEMA
            )
        try:
            items = json.loads(raw)["characters"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SurveyError(f"Resposta inesperada do modelo: {raw[:200]}") from exc
        for item in items:
            character = Character(
                name=str(item.get("name", "")).strip(),
                # Os modelos às vezes devolvem o nome com o tratamento (ミナちゃん), apesar da instrução
                original=_TRAILING_HONORIFIC.sub("", str(item.get("original", "")).strip()),
                gender=str(item.get("gender", "")).strip(),
                speech=str(item.get("speech", "")).strip(),
                notes=str(item.get("notes", "")).strip(),
            )
            if character.name:
                found.setdefault(character.name.casefold(), character)

    known_keys = {c.name.casefold() for c in known} | {c.original for c in known if c.original}
    return [c for c in found.values() if c.name.casefold() not in known_keys and (not c.original or c.original not in known_keys)]
