"""Per-install free-quota accounting.

The rule from the SRS: cache hits are free and unlimited; only a cache *miss*
that reaches the LLM costs one of the five free calls.

Concurrency note. The obvious implementation - read the counter, compare it to
the limit, call the LLM, then increment - has a race: a generation takes
seconds, and every concurrent request for the same install reads the same
pre-increment value and passes the check. Five parallel requests against a
one-call allowance all succeed.

So the counter is claimed up front with a single conditional UPDATE, which the
database evaluates atomically, and refunded if the generation then fails. The
SRS property "a failed generation never costs the user" is preserved, and the
window where two requests can both pass is gone.
"""

import logging

from sqlalchemy import update
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.models import UsageLog, utcnow

logger = logging.getLogger(__name__)


def get_or_create(
    session: Session, install_id: str, *, ip_address: str | None = None
) -> UsageLog:
    """Fetch this install's usage row, creating it on first sight.

    `ip_address` is recorded for later abuse-pattern review only. Per the SRS it
    never blocks a request: shared office, campus and carrier-NAT addresses mean
    many installs legitimately share one IP.
    """
    row = session.exec(select(UsageLog).where(UsageLog.install_id == install_id)).first()

    if row is None:
        row = UsageLog(install_id=install_id, ip_address=ip_address)
        session.add(row)
        try:
            session.commit()
        except Exception:
            # Two first-ever requests for one install can race on the unique
            # install_id. Whoever lost just re-reads the winner's row.
            session.rollback()
            row = session.exec(
                select(UsageLog).where(UsageLog.install_id == install_id)
            ).first()
            if row is None:
                raise
            return row
        session.refresh(row)
        logger.info("new install registered install_id=%s", install_id)
        return row

    if ip_address and row.ip_address != ip_address:
        # Keep the most recent address rather than a history; this is a coarse
        # signal for manual review, not an audit log.
        row.ip_address = ip_address
        session.add(row)
        session.commit()
        session.refresh(row)

    return row


def remaining(row: UsageLog, settings: Settings | None = None) -> int:
    """Free calls left, floored at zero."""
    settings = settings or get_settings()
    return max(0, settings.free_call_limit - row.free_llm_calls_used)


def try_reserve_call(
    session: Session, install_id: str, settings: Settings | None = None
) -> bool:
    """Atomically claim one free call. True if claimed, False if out of quota.

    The `WHERE free_llm_calls_used < limit` runs inside the UPDATE, so the
    database decides the winner. Concurrent callers cannot both see room.

    Call this *before* the LLM request, and `refund_call()` if it fails.
    """
    settings = settings or get_settings()

    result = session.execute(
        update(UsageLog)
        .where(UsageLog.install_id == install_id)
        .where(UsageLog.free_llm_calls_used < settings.free_call_limit)
        .values(
            free_llm_calls_used=UsageLog.free_llm_calls_used + 1,
            last_request_at=utcnow(),
        )
    )
    session.commit()

    claimed = result.rowcount > 0
    if claimed:
        logger.info("quota call reserved install_id=%s", install_id)
    else:
        logger.info("quota exhausted install_id=%s", install_id)
    return claimed


def refund_call(session: Session, install_id: str) -> None:
    """Give back a reserved call after a failed generation.

    Guarded with `> 0` so a double refund can never drive the counter negative.
    """
    session.execute(
        update(UsageLog)
        .where(UsageLog.install_id == install_id)
        .where(UsageLog.free_llm_calls_used > 0)
        .values(free_llm_calls_used=UsageLog.free_llm_calls_used - 1)
    )
    session.commit()
    logger.info("quota call refunded install_id=%s", install_id)


def touch(session: Session, install_id: str) -> None:
    """Update last-seen without spending quota (used on cache hits)."""
    session.execute(
        update(UsageLog)
        .where(UsageLog.install_id == install_id)
        .values(last_request_at=utcnow())
    )
    session.commit()
