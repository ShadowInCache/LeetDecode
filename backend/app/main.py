"""FastAPI application entrypoint.

Run locally:
    uvicorn app.main:app --reload --port 8000

A note on the try/except around `get_settings()`. Pydantic raises on any
malformed environment variable - `LLM_PROVIDER=groq`, `DAILY_JOB_HOUR=3am`,
`LLM_DAILY_CAP=1,000`. Because settings are built while this module is being
imported, an unguarded failure means uvicorn never starts: the platform serves
502s, `/health` is unreachable, and the only evidence is a traceback buried in
the deploy log. That is the worst possible shape for an operator - the service
looks dead rather than misconfigured.

So a configuration error no longer stops the process. It starts a deliberately
crippled app that answers every request with 503 and the actual error message,
and logs the same at startup. The service is still broken, but it now says
*why*, at the URL, in one request.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = None
config_error: Exception | None = None

try:
    settings = get_settings()
except Exception as exc:  # noqa: BLE001 - reported through the app, not a crash
    config_error = exc


def _safe_detail(error: Exception) -> str:
    """Describe a configuration failure without disclosing any value.

    `str(ValidationError)` embeds pydantic's `input_value` - the offending
    value itself. That is unacceptable here, because this text is served over
    HTTP without authentication and a misconfigured variable is very often a
    pasted secret. This happened for real: an API key ended up in
    `LLM_PROVIDER` and the error page served it to anyone who asked.

    So the detail is rebuilt from field names and messages only. The messages
    themselves are already written not to quote suspicious values (see
    `config._describe_value`).
    """
    errors = getattr(error, "errors", None)
    if not callable(errors):
        return f"{type(error).__name__}: configuration could not be loaded."

    lines = []
    for item in errors():
        field = ".".join(str(part) for part in item.get("loc", ())) or "(root)"
        lines.append(f"{field.upper()}: {item.get('msg', 'invalid value')}")
    return "\n".join(lines) or "Configuration could not be loaded."


def _build_misconfigured_app(error: Exception) -> FastAPI:
    """An app that does nothing but explain why it cannot run."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    detail = _safe_detail(error)
    logger.error("CONFIGURATION ERROR - the service cannot start normally:\n%s", detail)
    logger.error(
        "Fix the offending environment variable and redeploy. "
        "Every request will return 503 until then."
    )

    broken = FastAPI(
        title="LeetDecode API (misconfigured)",
        description="The service started but its configuration is invalid.",
    )
    # Permissive CORS so the message is readable from a browser or the popup.
    broken.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    @broken.get("/health")
    def health_misconfigured() -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "status": "misconfigured",
                "error": "INVALID_CONFIGURATION",
                "message": (
                    "The service is running but its environment variables are "
                    "invalid, so it cannot serve requests."
                ),
                "detail": detail,
            },
        )

    @broken.api_route(
        "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
    )
    def catch_all(path: str) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": "INVALID_CONFIGURATION",
                "message": "Service misconfigured - see /health for details.",
            },
        )

    return broken


if config_error is not None:
    app = _build_misconfigured_app(config_error)
else:
    from app.db import create_db_and_tables
    from app.observability import configure_logging, init_sentry
    from app.preflight import log_report, run as run_preflight
    from app.routers import admin, health, translate, usage
    from app.scheduler import shutdown_scheduler, start_scheduler

    # Logging is configured at import so even early messages are formatted.
    # Sentry is initialised in the lifespan instead: importing this module must
    # not have the side effect of wiring up a live error reporter, or simply
    # importing the app in a test sends events to the real project.
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Ensure the schema exists on boot.

        A database failure here is logged, not raised: /health must stay
        answerable so the platform can distinguish "process is up but the DB is
        unreachable" from "process is dead".
        """
        init_sentry(settings)

        try:
            create_db_and_tables()
        except Exception as exc:  # noqa: BLE001
            logger.error("database initialisation failed: %s", exc)

        # Say plainly what is and isn't configured. Without this, a variable
        # that never reached the service shows up only as translations failing.
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
    # origin, so CORS has to allow it. We don't use cookies or auth headers, so
    # a wildcard origin with credentials disabled is safe here.
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
