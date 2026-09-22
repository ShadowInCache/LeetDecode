"""Recording what each LLM call cost.

Kept separate from `llm/service.py` so the provider layer stays free of database
concerns: the service reports what happened, this decides how it is persisted
and logged.

Writes here are best-effort. A failure to record a call must never turn a
successful translation into an error for the user - the log exists for auditing,
not for correctness.
"""

import logging

from sqlmodel import Session

from app.config import ProviderName
from app.models import LLMCallLog
from app.observability import log_event
from app.pricing import estimate_cost_usd

logger = logging.getLogger(__name__)

# Where a call originated, for splitting user traffic from batch work.
SOURCE_TRANSLATE = "translate"
SOURCE_DAILY_JOB = "daily_job"
SOURCE_PRESEED = "preseed"


def record(
    session: Session | None,
    *,
    provider: ProviderName,
    model: str,
    source: str,
    install_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    succeeded: bool = True,
) -> float | None:
    """Log one LLM call and return its estimated cost in USD.

    `session` may be None, in which case the call is logged to stdout but not
    persisted - useful for call sites that have no database handle.
    """
    cost = estimate_cost_usd(model, input_tokens, output_tokens)

    log_event(
        logger,
        "llm_call",
        event="llm_call",
        provider=provider.value,
        model=model,
        source=source,
        install_id=install_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=round(cost, 8) if cost is not None else None,
        latency_ms=latency_ms,
        succeeded=succeeded,
    )

    if session is None:
        return cost

    try:
        session.add(
            LLMCallLog(
                provider=provider.value,
                model=model,
                source=source,
                install_id=install_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
                succeeded=succeeded,
            )
        )
        session.commit()
    except Exception:  # noqa: BLE001 - auditing must not break the request
        session.rollback()
        logger.exception("failed to persist llm_call_log row")

    return cost
