"""FastAPI application entrypoint.

Run locally:
    uvicorn app.main:app --reload --port 8000
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import create_db_and_tables
from app.observability import configure_logging, init_sentry
from app.preflight import log_report, run as run_preflight
from app.routers import admin, health, translate, usage
from app.scheduler import shutdown_scheduler, start_scheduler

settings = get_settings()

# Logging is configured at import so even early messages are formatted.
# Sentry is initialised in the lifespan instead: importing this module must not
# have the side effect of wiring up a live error reporter, or simply importing
# the app in a test sends events to the real project.
configure_logging(settings)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Ensure the schema exists on boot.

    A database failure here is logged, not raised: /health must stay answerable
    so the platform can distinguish "process is up but the DB is unreachable"
    from "process is dead".
    """
    init_sentry(settings)

    try:
        create_db_and_tables()
    except Exception as exc:  # noqa: BLE001
        logger.error("database initialisation failed: %s", exc)

    # Say plainly what is and isn't configured. Without this, a variable that
    # never reached the service shows up only as every translation failing.
    log_report(run_preflight(settings))

    start_scheduler(settings)
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(
    title="LeetDecode API",
    description="Translates LeetCode problem statements into plain English.",
    version="0.1.0",
    lifespan=lifespan,
)

# The Chrome extension popup calls this API from a `chrome-extension://<id>`
# origin, so CORS has to allow it. We don't use cookies or auth headers, so a
# wildcard origin with credentials disabled is safe here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

app.include_router(health.router)
app.include_router(translate.router)
app.include_router(usage.router)
app.include_router(admin.router)
