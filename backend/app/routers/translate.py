"""POST /translate - turn pasted LeetCode text into plain English."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session

from app import usage
from app.cache import find_cached, store_translation
from app.config import Settings, get_settings
from app.db import get_session
from app.llm import AllProvidersFailed, translate_problem
from app.schemas import SimplifiedProblem, TranslateRequest, TranslateResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["translate"])

QUOTA_MESSAGE = (
    "You've used your 5 free translations. Common problems remain free - try "
    "pasting one of LeetCode's top interview questions!"
)


def client_ip(request: Request) -> str | None:
    """Best-effort client IP.

    Railway and most PaaS proxies put the real address first in
    X-Forwarded-For. This is logged for abuse-pattern review only and never
    gates a request, so a spoofed value costs us nothing.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.post("/translate", response_model=TranslateResponse)
def translate(
    payload: TranslateRequest,
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TranslateResponse:
    # 1. Cache first, before any quota consideration. A hit is free and
    #    unlimited - that is what keeps the preseeded set free forever.
    cached = find_cached(session, payload.raw_text)
    if cached is not None:
        return TranslateResponse(
            source="cache",
            # Re-validate on the way out: a row could predate a schema change,
            # and we would rather fail loudly than ship a malformed shape.
            data=SimplifiedProblem.model_validate(cached.simplified_json),
        )

    # 2. Miss - this one costs quota, so check it before spending money.
    usage_row = usage.get_or_create(
        session, payload.install_id, ip_address=client_ip(request)
    )
    if not usage.has_quota(usage_row, settings):
        logger.info("quota exceeded install_id=%s", payload.install_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "QUOTA_EXCEEDED", "message": QUOTA_MESSAGE},
        )

    # 3. Generate.
    try:
        result = translate_problem(payload.raw_text, settings=settings)
    except AllProvidersFailed as exc:
        # Nothing is cached and no quota is spent - the failure is ours, not
        # the user's, so it must not cost them one of their five calls.
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

    # 4. Only now, with a validated result in hand, does the call count.
    store_translation(session, raw_text=payload.raw_text, problem=result.problem)
    usage.record_llm_call(session, usage_row)

    logger.info(
        "translated install_id=%s provider=%s", payload.install_id, result.provider.value
    )
    return TranslateResponse(source="llm", data=result.problem)
