"""Deferred write work that must not sit on the response path.

Some of what `/translate` records is not needed to answer the request:
`last_used_at`, the request counter, the cache-hit counter. Doing them inline
cost four extra round trips to the database, and against a managed Postgres
~56ms away that is most of a cache hit's latency.

FastAPI runs a `BackgroundTasks` callable *after* the response is sent, so
moving these here takes them off the user's clock entirely. They are batched
into one transaction with one commit, and failures are swallowed: bookkeeping
must never turn a served request into an error after the fact.
"""

import logging
import uuid

from sqlalchemy import update
from sqlmodel import Session

from app import ratelimit, stats
from app.db import get_engine
from app.models import ProblemCache, utcnow

logger = logging.getLogger(__name__)


def record_request_outcome(
    *,
    cache_hit: bool,
    cached_row_id: uuid.UUID | None = None,
) -> None:
    """Persist the per-request bookkeeping for one `/translate` call.

    Opens its own session: the request-scoped one is already closed by the time
    a background task runs.
    """
    try:
        with Session(get_engine()) as session:
            stats.bump(session, stats.REQUESTS_KEY, commit=False)
            if cache_hit:
                stats.bump(session, stats.CACHE_HITS_KEY, commit=False)

            if cached_row_id is not None:
                session.execute(
                    update(ProblemCache)
                    .where(ProblemCache.id == cached_row_id)
                    .values(last_used_at=utcnow())
                )

            # One commit for everything above, rather than one per counter.
            session.commit()
    except Exception:  # noqa: BLE001 - the response has already been sent
        logger.warning("background bookkeeping failed", exc_info=True)


def purge_old_rate_limit_windows() -> int:
    """Housekeeping hook for the daily job."""
    with Session(get_engine()) as session:
        return ratelimit.purge_expired(session)
