"""Chamadas à API da OpenAI (Responses API, saída em JSON com schema)."""

import base64

from .db import Character, Memory, Term
from .translators import (
    Line,
    Result,
    TranslationError,
    Usage,
    llm_instructions,
    parse_response,
    parse_terms,
    response_schema,
    text_request,
)


def cache_key(work_id: int | None) -> str | None:
    """Agrupa os pedidos de uma obra no mesmo servidor da OpenAI: sem isso o cache de prompt quase não acontece
    (testado: 0 tokens em cache sem a chave; ~90% da entrada em cache com ela)."""
    return f"mangaoverlay-obra-{work_id}" if work_id is not None else None


def request_body(model: str, instructions: str, input, schema: dict, name: str, prompt_cache_key: str | None = None) -> dict:
    """Parâmetros da Responses API; o mesmo corpo serve para a chamada normal e para a Batch API."""
    body = {
        "model": model,
        "instructions": instructions,
        "input": input,
        "text": {"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
    }
    if model.startswith("gpt-5"):
        # Modelos de raciocínio: o raciocínio é cobrado como saída; tarefa simples, esforço baixo
        body["reasoning"] = {"effort": "low"}
    if prompt_cache_key:
        body["prompt_cache_key"] = prompt_cache_key
    return body


def text_body(
    lines: list[Line],
    context: list[str],
    source: str,
    target: str,
    model: str,
    characters: list[Character] | None,
    memory: Memory | None = None,
    work_id: int | None = None,
) -> dict:
    """Corpo de um pedido de tradução no modo texto (envio normal e Batch API)."""
    return request_body(
        model,
        llm_instructions(source, target, False, characters, memory),
        text_request(lines, context),
        response_schema(False),
        "translations",
        cache_key(work_id),
    )


def parse_body(body: dict) -> tuple[str, Usage]:
    """Texto e uso de tokens de uma resposta da Responses API em forma de dicionário (arquivo de resultados do lote)."""
    text = "".join(
        part.get("text", "")
        for item in body.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )
    usage = body.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    return text, Usage(usage.get("input_tokens", 0), details.get("cached_tokens", 0) or 0, usage.get("output_tokens", 0))


def structured(
    api_key: str, model: str, instructions: str, input, schema: dict, name: str, prompt_cache_key: str | None = None
) -> tuple[str, Usage]:
    """Uma chamada com resposta em JSON garantida pelo schema. Retorna o texto JSON e o uso de tokens."""
    import openai

    client = openai.OpenAI(api_key=api_key, timeout=180)
    try:
        response = client.responses.create(**request_body(model, instructions, input, schema, name, prompt_cache_key))
    except openai.AuthenticationError as exc:
        raise TranslationError("Chave da API da OpenAI inválida.") from exc
    except openai.NotFoundError as exc:
        raise TranslationError(f"Modelo da OpenAI não encontrado: {model}") from exc
    except openai.RateLimitError as exc:
        # Sem crédito também vem como 429, mas não adianta tentar de novo
        quota = "insufficient_quota" in str(exc)
        message = "Sem crédito na conta da OpenAI." if quota else "Limite de requisições da OpenAI atingido."
        raise TranslationError(message, retryable=not quota) from exc
    except (openai.APIConnectionError, openai.APITimeoutError) as exc:
        raise TranslationError("Sem conexão com a API da OpenAI.", retryable=True) from exc
    except openai.InternalServerError as exc:
        raise TranslationError(f"Servidor da OpenAI com problema: {exc}", retryable=True) from exc
    except openai.APIError as exc:
        raise TranslationError(f"Erro na API da OpenAI: {exc}") from exc
    usage = Usage()
    if response.usage is not None:
        details = getattr(response.usage, "input_tokens_details", None)
        usage = Usage(
            response.usage.input_tokens,
            getattr(details, "cached_tokens", 0) or 0,
            response.usage.output_tokens,
        )
    return response.output_text, usage


def translate_text(
    lines: list[Line],
    context: list[str],
    source: str,
    target: str,
    model: str,
    api_key: str,
    characters: list[Character] | None = None,
    memory: Memory | None = None,
    work_id: int | None = None,
) -> tuple[list[Result], Usage, list[Term]]:
    body = text_body(lines, context, source, target, model, characters, memory, work_id)
    raw, usage = structured(
        api_key, model, body["instructions"], body["input"], response_schema(False), "translations", cache_key(work_id)
    )
    return parse_response(raw, lines), usage, parse_terms(raw)


def translate_image(
    page_jpeg: bytes,
    lines: list[Line],
    context: list[str],
    source: str,
    target: str,
    model: str,
    api_key: str,
    characters: list[Character] | None = None,
    memory: Memory | None = None,
    work_id: int | None = None,
) -> tuple[list[Result], Usage, list[Term]]:
    data_url = "data:image/jpeg;base64," + base64.b64encode(page_jpeg).decode()
    numbers = ", ".join(str(line.id) for line in lines)
    context_text = "\n".join(context) or "(none)"
    content = [
        {"type": "input_text", "text": f"Context from previous pages:\n{context_text}\n\nBoxes: {numbers}"},
        {"type": "input_image", "image_url": data_url},
    ]
    raw, usage = structured(
        api_key,
        model,
        llm_instructions(source, target, True, characters, memory),
        [{"role": "user", "content": content}],
        response_schema(True),
        "translations",
        cache_key(work_id),
    )
    return parse_response(raw, lines), usage, parse_terms(raw)
