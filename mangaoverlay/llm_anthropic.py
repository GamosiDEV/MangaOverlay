"""Tradução com a API do Claude (Anthropic), com saída em JSON garantida por schema.

Usa esforço baixo porque a tarefa é curta e o leitor está esperando, e o fallback do servidor
(`fallbacks: "default"`) para que uma recusa indevida de um classificador de segurança não
deixe a página sem tradução.
"""

import base64

from .translators import Line, Result, TranslationError, llm_instructions, parse_response, response_schema, text_request

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _call(api_key: str, model: str, system: str, content: list[dict], vision: bool) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=90)
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": response_schema(vision)},
            },
            betas=[_FALLBACK_BETA],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as exc:
        raise TranslationError("Chave da API do Claude inválida.") from exc
    except anthropic.NotFoundError as exc:
        raise TranslationError(f"Modelo do Claude não encontrado: {model}") from exc
    except anthropic.RateLimitError as exc:
        raise TranslationError("Limite de requisições da API do Claude atingido. Tente de novo em instantes.") from exc
    except anthropic.APIStatusError as exc:
        raise TranslationError(f"Erro na API do Claude: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise TranslationError("Sem conexão com a API do Claude.") from exc

    if response.stop_reason == "refusal":
        raise TranslationError("O Claude recusou traduzir esta tela.")
    if response.stop_reason == "max_tokens":
        raise TranslationError("A resposta do Claude foi cortada (texto demais na tela).")
    return next((block.text for block in response.content if block.type == "text"), "")


def translate_text(lines: list[Line], context: list[str], source: str, target: str, model: str, api_key: str) -> list[Result]:
    raw = _call(
        api_key,
        model,
        llm_instructions(source, target, vision=False),
        [{"type": "text", "text": text_request(lines, context)}],
        vision=False,
    )
    return parse_response(raw, lines)


def translate_image(
    page_jpeg: bytes, lines: list[Line], context: list[str], source: str, target: str, model: str, api_key: str
) -> list[Result]:
    numbers = ", ".join(str(line.id) for line in lines)
    context_text = "\n".join(context) or "(none)"
    raw = _call(
        api_key,
        model,
        llm_instructions(source, target, vision=True),
        [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(page_jpeg).decode()},
            },
            {"type": "text", "text": f"Context from previous pages:\n{context_text}\n\nBoxes: {numbers}"},
        ],
        vision=True,
    )
    return parse_response(raw, lines)
