"""Per-install free-quota accounting.

The rule from the SRS: cache hits are free and unlimited; only a cache *miss*
that reaches the LLM costs one of the five free calls. The counter is
incremented after a successful, validated generation - never before - so a
provider outage or a schema-invalid response does not burn a user's quota.
"""

import logging

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
        session.commit()
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


def has_quota(row: UsageLog, settings: Settings | None = None) -> bool:
    """True if this install may still spend a free LLM call."""
    settings = settings or get_settings()
    return row.free_llm_calls_used < settings.free_call_limit


def remaining(row: UsageLog, settings: Settings | None = None) -> int:
    """Free calls left, floored at zero."""
    settings = settings or get_settings()
    return max(0, settings.free_call_limit - row.free_llm_calls_used)


def record_llm_call(session: Session, row: UsageLog) -> UsageLog:
    """Increment the counter after a successful generation."""
    row.free_llm_calls_used += 1
    row.last_request_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    logger.info(
        "llm call recorded install_id=%s used=%d", row.install_id, row.free_llm_calls_used
    )
    return row


def touch(session: Session, row: UsageLog) -> None:
    """Update last-seen without spending quota (used on cache hits)."""
    row.last_request_at = utcnow()
    session.add(row)
    session.commit()
