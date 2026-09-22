"""GET /usage/{install_id} - remaining free quota.

Lets the popup display "3 of 5 free translations remaining" without guessing
client-side. The server stays the source of truth.
"""

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app import usage as usage_service
from app.config import Settings, get_settings
from app.db import get_session
from app.schemas import UsageResponse

router = APIRouter(tags=["usage"])


@router.get("/usage/{install_id}", response_model=UsageResponse)
def read_usage(
    install_id: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> UsageResponse:
    # Creating the row on a read is intentional: the popup calls this on open,
    # before the first translation, and an unknown install genuinely has the
    # full allowance. It keeps the endpoint free of a special "unknown" case.
    row = usage_service.get_or_create(session, install_id)
    return UsageResponse(
        free_calls_used=row.free_llm_calls_used,
        free_calls_remaining=usage_service.remaining(row, settings),
    )
