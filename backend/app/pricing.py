"""Per-model token pricing, used to estimate what each call cost.

These are list prices in USD per million tokens, recorded from each provider's
pricing page. They are a local snapshot, not a billed figure - treat the
resulting numbers as an estimate for spotting trends and abuse, not as an
invoice.

A model that is not in this table yields `None` rather than a guess: an absent
cost is honest, an invented one is not.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float


# Verified against the providers' published pricing, September 2026.
PRICES: dict[str, ModelPrice] = {
    # --- Google Gemini ---
    "gemini-3.1-flash-lite": ModelPrice(0.25, 1.50),
    "gemini-3.5-flash-lite": ModelPrice(0.30, 2.50),
    "gemini-3.5-flash": ModelPrice(1.50, 9.00),
    "gemini-2.5-flash": ModelPrice(0.30, 2.50),
    "gemini-2.5-flash-lite": ModelPrice(0.10, 0.40),
    # --- Groq (GroqCloud) ---
    "openai/gpt-oss-120b": ModelPrice(0.15, 0.60),
    "openai/gpt-oss-20b": ModelPrice(0.075, 0.30),
}

_warned_unknown: set[str] = set()


def estimate_cost_usd(
    model: str, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """Estimated USD cost of one call, or None if it can't be computed.

    Returns None when the model has no price on file or the provider didn't
    report token counts.
    """
    if input_tokens is None or output_tokens is None:
        return None

    price = PRICES.get(model)
    if price is None:
        # Warn once per unknown model rather than on every call.
        if model not in _warned_unknown:
            _warned_unknown.add(model)
            logger.warning(
                "no price on file for model %r - cost will be logged as unknown. "
                "Add it to app/pricing.py to get cost estimates.",
                model,
            )
        return None

    return (
        input_tokens / 1_000_000 * price.input_per_mtok
        + output_tokens / 1_000_000 * price.output_per_mtok
    )
