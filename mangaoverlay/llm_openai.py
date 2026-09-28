"""Chamadas à API da OpenAI (Responses API, saída em JSON com schema)."""

import base64

from .db import Character
from .translators import Line, Result, TranslationError, Usage, llm_instructions, parse_response, response_schema, text_request


def structured(api_key: str, model: str, instructions: str, input, schema: dict, name: str) -> tuple[str, Usage]:
    """Uma chamada com resposta em JSON garantida pelo schema. Retorna o texto JSON e o uso de tokens."""
    import openai

    kwargs = {}
    if model.startswith("gpt-5"):
        # Modelos de raciocínio: o raciocínio é cobrado como saída; tarefa simples, esforço baixo
        kwargs["reasoning"] = {"effort": "low"}
    client = openai.OpenAI(api_key=api_key, timeout=180)
    try:
        response = client.responses.create(
            model=model,
            instructions=instructions,
            input=input,
            text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
            **kwargs,
        )
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
    lines: list[Line], context: list[str], source: str, target: str, model: str, api_key: str, characters: list[Character] | None = None
) -> tuple[list[Result], Usage]:
    raw, usage = structured(
        api_key, model, llm_instructions(source, target, False, characters), text_request(lines, context), response_schema(False), "translations"
    )
    return parse_response(raw, lines), usage


def translate_image(
    page_jpeg: bytes,
    lines: list[Line],
    context: list[str],
    source: str,
    target: str,
    model: str,
    api_key: str,
    characters: list[Character] | None = None,
) -> tuple[list[Result], Usage]:
    data_url = "data:image/jpeg;base64," + base64.b64encode(page_jpeg).decode()
    numbers = ", ".join(str(line.id) for line in lines)
    context_text = "\n".join(context) or "(none)"
    content = [
        {"type": "input_text", "text": f"Context from previous pages:\n{context_text}\n\nBoxes: {numbers}"},
        {"type": "input_image", "image_url": data_url},
    ]
    raw, usage = structured(
        api_key, model, llm_instructions(source, target, True, characters), [{"role": "user", "content": content}], response_schema(True), "translations"
    )
    return parse_response(raw, lines), usage
