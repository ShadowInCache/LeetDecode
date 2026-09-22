"""Aggregates for the admin dashboard.

Two sources:

* `llm_call_log` - the authoritative record of what was actually spent.
* Daily counters in `rate_limit_bucket` - cheap tallies of total requests and
  cache hits. `llm_call_log` cannot provide these, because a cache hit never
  produces a call and so leaves no row. Reusing that table is deliberate: it is
  already a keyed window counter, and a second near-identical table would earn
  nothing.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import ratelimit
from app.config import Settings
from app.models import LLMCallLog, ProblemCache, RateLimitBucket, UsageLog

logger = logging.getLogger(__name__)

REQUESTS_KEY = "stats:requests"
CACHE_HITS_KEY = "stats:cache_hits"


def _bump(session: Session, key: str) -> None:
    """Increment a daily counter. Best-effort: never breaks a request."""
    start = ratelimit.window_start_for(datetime.now(timezone.utc), ratelimit.DAY_SECONDS)
    try:
        updated = session.execute(
            update(RateLimitBucket)
            .where(RateLimitBucket.bucket_key == key)
            .where(RateLimitBucket.window_start == start)
            .values(count=RateLimitBucket.count + 1)
        )
        session.commit()
        if updated.rowcount == 0:
            session.add(RateLimitBucket(bucket_key=key, window_start=start, count=1))
            try:
                session.commit()
            except IntegrityError:
                # Lost the create race; the other writer's increment stands.
                session.rollback()
    except Exception:  # noqa: BLE001 - a stats counter must not break traffic
        session.rollback()
        logger.debug("failed to bump counter %s", key, exc_info=True)


def record_request(session: Session) -> None:
    _bump(session, REQUESTS_KEY)


def record_cache_hit(session: Session) -> None:
    _bump(session, CACHE_HITS_KEY)


def _daily_counter(session: Session, key: str, day: datetime) -> int:
    start = ratelimit.window_start_for(day, ratelimit.DAY_SECONDS)
    row = session.exec(
        select(RateLimitBucket)
        .where(RateLimitBucket.bucket_key == key)
        .where(RateLimitBucket.window_start == start)
    ).first()
    return row.count if row else 0


def _calls_and_cost(session: Session, start: datetime, end: datetime | None = None):
    statement = (
        select(
            func.count(LLMCallLog.id),
            func.coalesce(func.sum(LLMCallLog.cost_usd), 0.0),
        )
        .where(LLMCallLog.created_at >= start)
        .where(LLMCallLog.succeeded.is_(True))
    )
    if end is not None:
        statement = statement.where(LLMCallLog.created_at < end)
    calls, cost = session.exec(statement).one()
    return calls or 0, float(cost or 0)


def collect(session: Session, settings: Settings, *, days: int = 14) -> dict[str, Any]:
    """Everything the dashboard renders, in one pass."""
    now = datetime.now(timezone.utc)
    today_start = ratelimit.window_start_for(now, ratelimit.DAY_SECONDS)
    window_start = today_start - timedelta(days=days - 1)

    today_calls, today_cost = _calls_and_cost(session, today_start)
    today_requests = _daily_counter(session, REQUESTS_KEY, now)
    today_hits = _daily_counter(session, CACHE_HITS_KEY, now)

    cap_used = ratelimit.current_usage(
        session, key=ratelimit.GLOBAL_LLM_KEY, window_seconds=ratelimit.DAY_SECONDS
    )

    series: list[dict[str, Any]] = []
    for offset in range(days):
        day = window_start + timedelta(days=offset)
        calls, cost = _calls_and_cost(session, day, day + timedelta(days=1))
        series.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "calls": calls,
                "cost_usd": round(cost, 6),
                "requests": _daily_counter(session, REQUESTS_KEY, day),
                "cache_hits": _daily_counter(session, CACHE_HITS_KEY, day),
            }
        )

    providers = [
        {
            "provider": provider,
            "model": model,
            "calls": calls,
            "cost_usd": round(float(cost or 0), 6),
            "avg_latency_ms": int(latency or 0),
        }
        for provider, model, calls, cost, latency in session.exec(
            select(
                LLMCallLog.provider,
                LLMCallLog.model,
                func.count(LLMCallLog.id),
                func.coalesce(func.sum(LLMCallLog.cost_usd), 0.0),
                func.avg(LLMCallLog.latency_ms),
            )
            .where(LLMCallLog.created_at >= window_start)
            .group_by(LLMCallLog.provider, LLMCallLog.model)
        ).all()
    ]

    # A coarse abuse signal: one install doing far more than the rest.
    top_installs = [
        {
            "install_id": install_id,
            "calls": calls,
            "cost_usd": round(float(cost or 0), 6),
        }
        for install_id, calls, cost in session.exec(
            select(
                LLMCallLog.install_id,
                func.count(LLMCallLog.id),
                func.coalesce(func.sum(LLMCallLog.cost_usd), 0.0),
            )
            .where(LLMCallLog.created_at >= window_start)
            .where(LLMCallLog.install_id.is_not(None))
            .group_by(LLMCallLog.install_id)
            .order_by(func.count(LLMCallLog.id).desc())
            .limit(10)
        ).all()
    ]

    cache_total = session.exec(select(func.count(ProblemCache.id))).one() or 0
    cache_preseeded = (
        session.exec(
            select(func.count(ProblemCache.id)).where(
                ProblemCache.is_preseeded.is_(True)
            )
        ).one()
        or 0
    )
    installs_total = session.exec(select(func.count(UsageLog.id))).one() or 0
    installs_exhausted = (
        session.exec(
            select(func.count(UsageLog.id)).where(
                UsageLog.free_llm_calls_used >= settings.free_call_limit
            )
        ).one()
        or 0
    )
    failures = (
        session.exec(
            select(func.count(LLMCallLog.id))
            .where(LLMCallLog.created_at >= window_start)
            .where(LLMCallLog.succeeded.is_(False))
        ).one()
        or 0
    )

    period_cost = sum(d["cost_usd"] for d in series)
    period_calls = sum(d["calls"] for d in series)

    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "today": {
            "requests": today_requests,
            "cache_hits": today_hits,
            "llm_calls": today_calls,
            "cost_usd": round(today_cost, 6),
            "cache_hit_rate": (
                round(today_hits / today_requests, 4) if today_requests else None
            ),
            "cap_used": cap_used,
            "cap_limit": settings.llm_daily_cap,
            "cap_pct": (
                round(cap_used / settings.llm_daily_cap * 100, 1)
                if settings.llm_daily_cap
                else 0.0
            ),
        },
        "period": {
            "cost_usd": round(period_cost, 6),
            "calls": period_calls,
            "failures": failures,
            "avg_cost_per_call": (
                round(period_cost / period_calls, 8) if period_calls else None
            ),
        },
        "series": series,
        "providers": providers,
        "top_installs": top_installs,
        "cache": {
            "total": cache_total,
            "preseeded": cache_preseeded,
            "organic": cache_total - cache_preseeded,
        },
        "installs": {
            "total": installs_total,
            "quota_exhausted": installs_exhausted,
        },
    }
