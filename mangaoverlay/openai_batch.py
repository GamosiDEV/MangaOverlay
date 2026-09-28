"""Cliente da Batch API da OpenAI: envia um arquivo JSONL de pedidos, acompanha e baixa os resultados.

50% mais barata que as chamadas normais; a OpenAI processa em segundo plano (em geral em minutos,
com garantia de até 24 h). Cada pedido tem resultado próprio: os que falham vêm num arquivo de erros.
"""

import json
from dataclasses import dataclass

from .translators import TranslationError

ENDPOINT = "/v1/responses"
DISCOUNT = 0.5
WAITING = {"validating", "in_progress", "finalizing", "cancelling"}
FINISHED = {"completed", "expired", "cancelled"}  # têm resultados (parciais, se expirou ou foi cancelado)


@dataclass
class RemoteStatus:
    status: str
    completed: int
    failed: int
    total: int
    output_file_id: str | None
    error_file_id: str | None
    errors: list[str]


def _client(api_key: str):
    import openai

    return openai.OpenAI(api_key=api_key, timeout=120)


def _call(fn):
    import openai

    try:
        return fn()
    except openai.AuthenticationError as exc:
        raise TranslationError("Chave da API da OpenAI inválida.") from exc
    except (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError, openai.RateLimitError) as exc:
        raise TranslationError(f"OpenAI indisponível no momento: {exc}", retryable=True) from exc
    except openai.APIError as exc:
        raise TranslationError(f"Erro na Batch API da OpenAI: {exc}") from exc


def jsonl(requests: dict[str, dict]) -> bytes:
    """custom_id -> corpo do pedido; uma linha JSON por pedido."""
    return "".join(
        json.dumps({"custom_id": custom_id, "method": "POST", "url": ENDPOINT, "body": body}, ensure_ascii=False) + "\n"
        for custom_id, body in requests.items()
    ).encode()


def submit(api_key: str, requests: dict[str, dict]) -> str:
    """Envia os pedidos e cria o lote na OpenAI. Retorna o id do lote remoto."""
    client = _client(api_key)
    uploaded = _call(lambda: client.files.create(file=("mangaoverlay.jsonl", jsonl(requests)), purpose="batch"))
    batch = _call(lambda: client.batches.create(input_file_id=uploaded.id, endpoint=ENDPOINT, completion_window="24h"))
    return batch.id


def status(api_key: str, remote_id: str) -> RemoteStatus:
    batch = _call(lambda: _client(api_key).batches.retrieve(remote_id))
    counts = batch.request_counts
    errors = [f"{e.code}: {e.message}" for e in (batch.errors.data if batch.errors and batch.errors.data else [])]
    return RemoteStatus(
        batch.status,
        counts.completed if counts else 0,
        counts.failed if counts else 0,
        counts.total if counts else 0,
        batch.output_file_id,
        batch.error_file_id,
        errors,
    )


def download(api_key: str, file_id: str | None) -> list[dict]:
    """Linhas do arquivo de resultados ou de erros (lista vazia se não houver arquivo)."""
    if not file_id:
        return []
    text = _call(lambda: _client(api_key).files.content(file_id).text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def cancel(api_key: str, remote_id: str) -> None:
    _call(lambda: _client(api_key).batches.cancel(remote_id))
