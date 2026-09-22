"""Fixed-window rate limiting and the global spend cap.

Why this exists: `install_id` is generated client-side and unauthenticated, so
the five-call quota is trivially reset by minting a fresh UUID. Without a second
layer, `/translate` is an open, unmetered door to a paid API. These counters are
that layer.

Three kinds of limit, each a counter in `rate_limit_bucket`:

  req:ip:<ip>      every request        - coarse volumetric brake
  llm:ip:<ip>      cache misses only    - the actual cost brake
  install:ip:<ip>  new install_ids      - closes the quota-reset vector
  llm:global       LLM calls per day    - hard ceiling on the bill

Fixed windows, not sliding: a sliding window needs per-request timestamps and a
range scan, and the extra precision buys nothing here. The worst case is a
client getting up to 2x the limit across a window boundary, which for a cost
brake is fine.

Atomicity follows the same pattern as the quota counter: the limit lives in the
UPDATE's WHERE clause, so the database decides the winner and there is no
read-then-write window for concurrent requests to slip through.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import RateLimitBucket

logger = logging.getLogger(__name__)

HOUR_SECONDS = 3600
DAY_SECONDS = 86_400


@dataclass(frozen=True)
class LimitResult:
    """Outcome of trying to consume one unit from a bucket."""

    allowed: bool
    limit: int
    used: int
    retry_after_seconds: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def window_start_for(now: datetime, window_seconds: int) -> datetime:
    """Truncate `now` down to the start of its fixed window."""
    epoch_seconds = int(now.timestamp())
    return datetime.fromtimestamp(
        epoch_seconds - (epoch_seconds % window_seconds), tz=timezone.utc
    )


def _seconds_until_window_end(
    now: datetime, window_start: datetime, window_seconds: int
) -> int:
    elapsed = (now - window_start).total_seconds()
    return max(1, int(window_seconds - elapsed))


def consume(
    session: Session,
    *,
    key: str,
    limit: int,
    window_seconds: int,
    now: datetime | None = None,
) -> LimitResult:
    """Take one unit from `key`'s current window.

    Returns `allowed=False` without incrementing when the limit is already
    reached, so a client that keeps hammering cannot push its own window out.
    """
    now = now or datetime.now(timezone.utc)
    start = window_start_for(now, window_seconds)
    retry_after = _seconds_until_window_end(now, start, window_seconds)

    # Fast path: the row exists and has room. The `count < limit` predicate is
    # evaluated by the database, so two concurrent callers cannot both take the
    # last unit.
    claimed = session.execute(
        update(RateLimitBucket)
        .where(RateLimitBucket.bucket_key == key)
        .where(RateLimitBucket.window_start == start)
        .where(RateLimitBucket.count < limit)
        .values(count=RateLimitBucket.count + 1)
    )
    session.commit()
    if claimed.rowcount > 0:
        used = _current_count(session, key, start)
        return LimitResult(True, limit, used, retry_after)

    # Either the row is missing (first request in this window) or it is full.
    existing = _current_count(session, key, start)
    if existing > 0:
        return LimitResult(False, limit, existing, retry_after)

    # First request in this window - create the row.
    session.add(RateLimitBucket(bucket_key=key, window_start=start, count=1))
    try:
        session.commit()
    except IntegrityError:
        # Another request created the row between our check and this insert.
        # Retry the conditional update once; it now has a row to work against.
        session.rollback()
        retried = session.execute(
            update(RateLimitBucket)
            .where(RateLimitBucket.bucket_key == key)
            .where(RateLimitBucket.window_start == start)
            .where(RateLimitBucket.count < limit)
            .values(count=RateLimitBucket.count + 1)
        )
        session.commit()
        used = _current_count(session, key, start)
        return LimitResult(retried.rowcount > 0, limit, used, retry_after)

    return LimitResult(True, limit, 1, retry_after)


def refund(
    session: Session, *, key: str, window_seconds: int, now: datetime | None = None
) -> None:
    """Give back one consumed unit.

    Used when a reserved LLM call never happened, so a provider outage doesn't
    eat into the daily cap.
    """
    now = now or datetime.now(timezone.utc)
    start = window_start_for(now, window_seconds)
    session.execute(
        update(RateLimitBucket)
        .where(RateLimitBucket.bucket_key == key)
        .where(RateLimitBucket.window_start == start)
        .where(RateLimitBucket.count > 0)
        .values(count=RateLimitBucket.count - 1)
    )
    session.commit()


def current_usage(
    session: Session, *, key: str, window_seconds: int, now: datetime | None = None
) -> int:
    """Read a bucket's count without consuming from it."""
    now = now or datetime.now(timezone.utc)
    return _current_count(session, key, window_start_for(now, window_seconds))


def _current_count(session: Session, key: str, start: datetime) -> int:
    row = session.exec(
        select(RateLimitBucket)
        .where(RateLimitBucket.bucket_key == key)
        .where(RateLimitBucket.window_start == start)
    ).first()
    return row.count if row else 0


def purge_expired(session: Session, *, older_than_seconds: int = 2 * DAY_SECONDS) -> int:
    """Delete windows that have long since closed.

    Called from the daily job. Without it the table grows one row per IP per
    window forever; the counts themselves are meaningless once the window ends.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    result = session.execute(
        delete(RateLimitBucket).where(RateLimitBucket.window_start < cutoff)
    )
    session.commit()
    removed = result.rowcount or 0
    if removed:
        logger.info("purged %d expired rate-limit buckets", removed)
    return removed


# ---------------------------------------------------------------------------
# Key builders - keep the namespacing in one place
# ---------------------------------------------------------------------------


def request_key(ip: str) -> str:
    return f"req:ip:{ip}"


def llm_key(ip: str) -> str:
    return f"llm:ip:{ip}"


def new_install_key(ip: str) -> str:
    return f"install:ip:{ip}"


GLOBAL_LLM_KEY = "llm:global"
