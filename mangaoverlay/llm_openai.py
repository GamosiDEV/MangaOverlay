"""Chamadas à API da OpenAI (Responses API, saída em JSON com schema)."""

import base64

from .db import Character
from .translators import Line, Result, TranslationError, llm_instructions, parse_response, response_schema, text_request


def structured(api_key: str, model: str, instructions: str, input, schema: dict, name: str) -> str:
    """Uma chamada com resposta em JSON garantida pelo schema. Retorna o texto JSON."""
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
    except openai.APIConnectionError as exc:
        raise TranslationError("Sem conexão com a API da OpenAI.") from exc
    except openai.APIError as exc:
        raise TranslationError(f"Erro na API da OpenAI: {exc}") from exc
    return response.output_text


def translate_text(
    lines: list[Line], context: list[str], source: str, target: str, model: str, api_key: str, characters: list[Character] | None = None
) -> list[Result]:
    raw = structured(
        api_key, model, llm_instructions(source, target, False, characters), text_request(lines, context), response_schema(False), "translations"
    )
    return parse_response(raw, lines)


def translate_image(
    page_jpeg: bytes,
    lines: list[Line],
    context: list[str],
    source: str,
    target: str,
    model: str,
    api_key: str,
    characters: list[Character] | None = None,
) -> list[Result]:
    data_url = "data:image/jpeg;base64," + base64.b64encode(page_jpeg).decode()
    numbers = ", ".join(str(line.id) for line in lines)
    context_text = "\n".join(context) or "(none)"
    content = [
        {"type": "input_text", "text": f"Context from previous pages:\n{context_text}\n\nBoxes: {numbers}"},
        {"type": "input_image", "image_url": data_url},
    ]
    raw = structured(
        api_key, model, llm_instructions(source, target, True, characters), [{"role": "user", "content": content}], response_schema(True), "translations"
    )
    return parse_response(raw, lines)
