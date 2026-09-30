"""Memória da obra: resumo da história e glossário, usados em toda tradução com IA daquela obra.

A memória que vai nos pedidos é uma "foto" que só muda em pontos fixos (fim de capítulo no lote, ou a
cada SCREEN_CONSOLIDATE_EVERY termos novos na leitura pela tela). Assim o começo dos pedidos fica
idêntico entre um bloco e outro e o cache de prompt funciona (entrada em cache custa 10–25% do preço).

O resumo é atualizado quando um capítulo termina de ser traduzido, com o modelo barato do levantamento
de nomes e só com o texto já traduzido (sem o original): cerca de US$ 0,001 por capítulo.
"""

import json
from dataclasses import dataclass

from . import credentials, llm_anthropic, llm_openai, pricing
from .config import Config
from .db import Database, TranslationKey
from .languages import target_english
from .translators import TranslationError, Usage

GLOSSARY_LIMIT = 80  # termos na foto (~1.200 tokens)
SUMMARY_WORDS = 180
SCREEN_CONSOLIDATE_EVERY = 15

_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
# Tokens de saída estimados por resumo (texto + raciocínio nos modelos gpt-5)
_SUMMARY_OUTPUT_TOKENS = 700


@dataclass
class SummaryResult:
    chapters: list[str]
    usage: Usage
    cost: float
    error: str | None = None  # o resumo falhou (tenta de novo quando o próximo capítulo terminar)


def summary_model(config: Config, engine: str) -> str | None:
    """Modelo barato do mesmo provedor do lote (o do levantamento completo de nomes)."""
    if engine == "openai-text":
        return config.names_model_openai
    if engine == "claude-text":
        return config.names_model_claude
    return None  # NLLB/Google não usam memória


def estimate_summary_cost(config: Config, engine: str, translated_tokens_by_chapter: list[int]) -> float | None:
    model = summary_model(config, engine)
    if model is None or not config.memory_summary or not translated_tokens_by_chapter:
        return 0.0
    input_tokens = sum(t + 600 for t in translated_tokens_by_chapter)
    return pricing.cost(model, input_tokens, _SUMMARY_OUTPUT_TOKENS * len(translated_tokens_by_chapter))


def _instructions(target: str, previous: str) -> str:
    return (
        "You keep the running summary of a comic series for a translator. You receive the summary so far and the "
        f"dialogue of the next chapter, already translated into {target_english(target)}. Write the updated summary "
        f"in {target_english(target)}, at most {SUMMARY_WORDS} words: main events, who is who, relationships and "
        "open plot threads that help translate the next chapters consistently. Keep what still matters from the "
        "previous summary and drop details that no longer do.\n\nSummary so far:\n" + (previous or "(empty: first chapter)")
    )


def summarize_chapters(db: Database, config: Config, key: TranslationKey) -> SummaryResult:
    """Para cada capítulo que acabou de ficar completo (em ordem): resume, se o resumo estiver ligado, e no fim
    atualiza a foto do glossário. Se nenhum capítulo terminou, não muda nada: a memória fica congelada durante o
    capítulo, e o começo dos pedidos continua idêntico (cache de prompt)."""
    usage, cost, done, error = Usage(), 0.0, [], None
    work_id = key.work_id
    model = summary_model(config, key.engine) if config.memory_summary else None
    for chapter_id, name, translations in db.chapters_to_summarize(work_id, key):
        if model is not None:
            previous = db.memory(work_id).summary
            text = "\n".join(translations)
            try:
                if key.engine == "openai-text":
                    raw, call_usage = llm_openai.structured(
                        credentials.get_key(credentials.OPENAI), model, _instructions(key.target, previous), text, _SUMMARY_SCHEMA, "summary"
                    )
                else:
                    raw, call_usage = llm_anthropic.structured(
                        credentials.get_key(credentials.ANTHROPIC), model, _instructions(key.target, previous),
                        [{"type": "text", "text": text}], _SUMMARY_SCHEMA,
                    )
                summary = str(json.loads(raw).get("summary", "")).strip()
            except (TranslationError, ValueError) as exc:
                error = f"{name}: {exc}"
                break  # o resumo é um extra: se falhar, tenta de novo quando o próximo capítulo terminar
            if summary:
                db.set_summary(work_id, summary)
            usage = usage + call_usage
            cost += pricing.cost(model, call_usage.input_tokens, call_usage.output_tokens, call_usage.cached_tokens) or 0.0
        db.mark_summarized(chapter_id)
        done.append(name)
    if done:
        db.consolidate_terms(work_id, GLOSSARY_LIMIT)
    return SummaryResult(done, usage, cost, error)
