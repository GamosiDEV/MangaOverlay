"""Preços das APIs (US$ por 1 milhão de tokens) e estimativa de tokens, para mostrar o custo antes de enviar.

Valores consultados em 28/09/2026 (developers.openai.com/api/docs/pricing e tabela de preços da Anthropic).
Modelos fora da tabela ficam sem estimativa.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: float
    cached_input: float
    output: float


PRICES = {
    "gpt-4.1-mini": Price(0.40, 0.10, 1.60),
    "gpt-4.1": Price(2.00, 0.50, 8.00),
    "gpt-5-mini": Price(0.25, 0.025, 2.00),
    "gpt-5": Price(1.25, 0.125, 10.00),
    "gpt-5.1": Price(1.25, 0.125, 10.00),
    "gpt-5.2": Price(1.75, 0.175, 14.00),
    "gpt-5.4-nano": Price(0.20, 0.02, 1.25),
    "gpt-5.4-mini": Price(0.75, 0.075, 4.50),
    # Anthropic: leitura de cache em ~10% do preço de entrada
    "claude-opus-5": Price(5.00, 0.50, 25.00),
    "claude-sonnet-5": Price(2.00, 0.20, 10.00),
    "claude-haiku-4-5": Price(1.00, 0.10, 5.00),
}


def estimate_tokens(text: str) -> int:
    """Aproximação: ~1 token por caractere japonês/chinês/coreano, ~4 caracteres por token em escrita latina."""
    wide = sum(1 for ch in text if ord(ch) > 0x2E80)
    return wide + (len(text) - wide) // 4 + 1


def cost(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0, discount: float = 1.0) -> float | None:
    """Custo em US$; `discount` 0,5 para a Batch API."""
    price = PRICES.get(model)
    if price is None:
        return None
    return (
        (input_tokens - cached_tokens) * price.input + cached_tokens * price.cached_input + output_tokens * price.output
    ) / 1_000_000 * discount


def format_cost(value: float | None) -> str:
    if value is None:
        return "custo desconhecido para este modelo"
    if value < 0.01:
        return "menos de US$ 0,01"
    return f"~US$ {value:.2f}".replace(".", ",")


def thousands(value: int) -> str:
    """123456 -> "123.456" (separador de milhar brasileiro)."""
    return f"{value:,}".replace(",", ".")
