"""Tradução com a API da OpenAI (Responses API, saída em JSON com schema)."""

import base64

from .translators import Line, Result, TranslationError, llm_instructions, parse_response, response_schema, text_request


def _call(api_key: str, **kwargs):
    import openai

    client = openai.OpenAI(api_key=api_key, timeout=90)
    try:
        return client.responses.create(**kwargs)
    except openai.AuthenticationError as exc:
        raise TranslationError("Chave da API da OpenAI inválida.") from exc
    except openai.APIConnectionError as exc:
        raise TranslationError("Sem conexão com a API da OpenAI.") from exc
    except openai.APIError as exc:
        raise TranslationError(f"Erro na API da OpenAI: {exc}") from exc


def _format(vision: bool) -> dict:
    return {"format": {"type": "json_schema", "name": "translations", "schema": response_schema(vision), "strict": True}}


def translate_text(lines: list[Line], context: list[str], source: str, target: str, model: str, api_key: str) -> list[Result]:
    response = _call(
        api_key,
        model=model,
        instructions=llm_instructions(source, target, vision=False),
        input=text_request(lines, context),
        text=_format(vision=False),
    )
    return parse_response(response.output_text, lines)


def translate_image(
    page_jpeg: bytes, lines: list[Line], context: list[str], source: str, target: str, model: str, api_key: str
) -> list[Result]:
    data_url = "data:image/jpeg;base64," + base64.b64encode(page_jpeg).decode()
    numbers = ", ".join(str(line.id) for line in lines)
    context_text = "\n".join(context) or "(none)"
    response = _call(
        api_key,
        model=model,
        instructions=llm_instructions(source, target, vision=True),
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"Context from previous pages:\n{context_text}\n\nBoxes: {numbers}"},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ],
        text=_format(vision=True),
    )
    return parse_response(response.output_text, lines)
