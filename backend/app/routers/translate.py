"""POST /translate - turn pasted LeetCode text into plain English.

Request flow, in the order the checks must happen:

  1. volumetric per-IP limit      -> 429   (cheap, before any work)
  2. cache lookup                 -> 200   free and unlimited, no quota, no cap
  3. per-IP cache-miss limit      -> 429   the cost brake
  4. new-install-per-IP limit     -> 429   closes the quota-reset vector
  5. global daily cap             -> 503   cache-only mode, bill can't run away
  6. per-install quota            -> 403   the five free calls
  7. generate, then cache

The cache lookup sits above everything that costs money on purpose: a hit is
free for everyone forever, which is what makes the preseeded set work.
"""

import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from sqlmodel import Session, select

from app import bookkeeping, call_log, ratelimit, usage
from app.cache import find_cached, store_translation
from app.config import Settings, get_settings
from app.db import get_session
from app.llm import AllProvidersFailed, translate_problem
from app.models import UsageLog
from app.schemas import SimplifiedProblem, TranslateRequest, TranslateResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["translate"])

QUOTA_MESSAGE = (
    "You've used your 5 free translations. Common problems remain free - try "
    "pasting one of LeetCode's top interview questions!"
)
RATE_LIMITED_MESSAGE = (
    "You're going a bit fast for us. Common problems are still free - try again "
    "in a few minutes."
)
AT_CAPACITY_MESSAGE = (
    "LeetDecode has hit its daily limit for new translations. Previously "
    "simplified problems still work - try one of LeetCode's top questions."
)


def client_ip(request: Request) -> str:
    """Best-effort client IP.

    Railway and most PaaS proxies put the real address first in
    X-Forwarded-For. A spoofed value only lets an attacker spread themselves
    across buckets - the global daily cap below is the backstop that does not
    depend on the IP being honest.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _too_many_requests(result: ratelimit.LimitResult, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"error": "RATE_LIMITED", "message": message},
        headers={"Retry-After": str(result.retry_after_seconds)},
    )


def _install_exists(session: Session, install_id: str) -> bool:
    return (
        session.exec(select(UsageLog).where(UsageLog.install_id == install_id)).first()
        is not None
    )


@router.post("/translate", response_model=TranslateResponse)
def translate(
    payload: TranslateRequest,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TranslateResponse:
    ip = client_ip(request)
    limits_on = settings.rate_limits_enabled

    # 1. Volumetric brake, before any database or provider work.
    if limits_on:
        result = ratelimit.consume(
            session,
            key=ratelimit.request_key(ip),
            limit=settings.rate_limit_requests_per_hour,
            window_seconds=ratelimit.HOUR_SECONDS,
        )
        if not result.allowed:
            logger.warning("rate limited (requests) ip=%s", ip)
            raise _too_many_requests(result, RATE_LIMITED_MESSAGE)

    # 2. Cache. Free, unlimited, and never counted against anything.
    cached = find_cached(session, payload.raw_text)
    if cached is not None:
        # Everything this request still owes the database - the request
        # counter, the cache-hit counter, last_used_at - is bookkeeping the
        # caller does not wait for. Deferring it keeps a cache hit at one
        # round trip instead of five.
        background.add_task(
            bookkeeping.record_request_outcome,
            cache_hit=True,
            cached_row_id=cached.id,
        )
        return TranslateResponse(
            source="cache",
            # Re-validate on the way out: a row could predate a schema change,
            # and we would rather fail loudly than ship a malformed shape.
            data=SimplifiedProblem.model_validate(cached.simplified_json),
        )

    background.add_task(bookkeeping.record_request_outcome, cache_hit=False)

    # From here the request may cost money.
    is_new_install = not _install_exists(session, payload.install_id)

    if limits_on:
        # 3. Per-IP cost brake, counted only on cache misses.
        miss_result = ratelimit.consume(
            session,
            key=ratelimit.llm_key(ip),
            limit=settings.rate_limit_llm_per_hour,
            window_seconds=ratelimit.HOUR_SECONDS,
        )
        if not miss_result.allowed:
            logger.warning("rate limited (llm) ip=%s", ip)
            raise _too_many_requests(miss_result, RATE_LIMITED_MESSAGE)

        # 4. New-install brake. Minting fresh UUIDs is how the per-install
        #    quota gets defeated, so registrations are themselves limited.
        if is_new_install:
            install_result = ratelimit.consume(
                session,
                key=ratelimit.new_install_key(ip),
                limit=settings.rate_limit_new_installs_per_hour,
                window_seconds=ratelimit.HOUR_SECONDS,
            )
            if not install_result.allowed:
                logger.warning(
                    "rate limited (new installs) ip=%s install_id=%s",
                    ip,
                    payload.install_id,
                )
                raise _too_many_requests(install_result, RATE_LIMITED_MESSAGE)

    # 5. Global daily ceiling. Reserved up front and refunded on failure, so it
    #    is a genuine hard cap rather than a best-effort count.
    global_reserved = False
    if limits_on:
        cap_result = ratelimit.consume(
            session,
            key=ratelimit.GLOBAL_LLM_KEY,
            limit=settings.llm_daily_cap,
            window_seconds=ratelimit.DAY_SECONDS,
        )
        if not cap_result.allowed:
            logger.error(
                "daily LLM cap reached (%d) - serving cache only", settings.llm_daily_cap
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "AT_CAPACITY", "message": AT_CAPACITY_MESSAGE},
                headers={"Retry-After": str(cap_result.retry_after_seconds)},
            )
        global_reserved = True

    def release_global() -> None:
        if global_reserved:
            ratelimit.refund(
                session,
                key=ratelimit.GLOBAL_LLM_KEY,
                window_seconds=ratelimit.DAY_SECONDS,
            )

    # 6. Per-install quota, claimed atomically before any spend.
    usage.get_or_create(session, payload.install_id, ip_address=ip)
    if not usage.try_reserve_call(session, payload.install_id, settings):
        release_global()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "QUOTA_EXCEEDED", "message": QUOTA_MESSAGE},
        )

    # 7. Generate. Everything reserved above must be released on failure.
    try:
        result = translate_problem(payload.raw_text, settings=settings)
    except AllProvidersFailed as exc:
        usage.refund_call(session, payload.install_id)
        release_global()
        logger.error("translation failed for install_id=%s: %s", payload.install_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "TRANSLATION_FAILED",
                "message": (
                    "We couldn't simplify that problem just now. "
                    "Please try again in a moment."
                ),
            },
        ) from exc
    except Exception:
        # Anything unexpected is still our fault, not the user's.
        usage.refund_call(session, payload.install_id)
        release_global()
        raise

    store_translation(session, raw_text=payload.raw_text, problem=result.problem)

    call_log.record(
        session,
        provider=result.provider,
        model=result.model,
        source=call_log.SOURCE_TRANSLATE,
        install_id=payload.install_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
    )
    return TranslateResponse(source="llm", data=result.problem)
