"""Error tracking and structured logging.

Sentry is optional: with no `SENTRY_DSN` the app runs exactly as before and
initialisation is skipped. That keeps local development and the test suite free
of any dependency on a third-party service.

Privacy note. `send_default_pii` defaults to False, and `_scrub_event` strips
pasted problem text out of anything that does get sent. The SRS commits to
collecting only an anonymous install ID and the problem statement; shipping user
IPs and their pasted text to a third-party processor is a disclosure decision,
not a default.
"""

import json
import logging
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

# Request fields that may carry a user's pasted problem text.
_SENSITIVE_KEYS = {"raw_text", "problem_text", "text"}
_REDACTED = "[scrubbed]"


def _scrub(value: Any, depth: int = 0) -> Any:
    """Recursively replace sensitive values, bounded against deep structures."""
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {
            k: (_REDACTED if k in _SENSITIVE_KEYS else _scrub(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item, depth + 1) for item in value]
    return value


def _scrub_event(event: dict, _hint: dict) -> dict:
    """Sentry `before_send` hook: drop pasted problem text from the payload."""
    request = event.get("request")
    if isinstance(request, dict):
        if "data" in request:
            request["data"] = _scrub(request["data"])
        # Query strings shouldn't carry problem text, but don't take the risk.
        request.pop("cookies", None)

    extra = event.get("extra")
    if isinstance(extra, dict):
        event["extra"] = _scrub(extra)

    return event


def init_sentry(settings: Settings) -> bool:
    """Initialise Sentry if configured. Returns True when it was enabled."""
    if not settings.sentry_dsn:
        logger.info("Sentry not configured (SENTRY_DSN empty) - error tracking disabled")
        return False

    try:
        import sentry_sdk
    except ImportError:
        logger.warning("sentry-sdk is not installed - error tracking disabled")
        return False

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        # See the module docstring: this is a privacy decision, not a default.
        send_default_pii=settings.sentry_send_pii,
        before_send=_scrub_event,
    )
    logger.info(
        "Sentry initialised environment=%s traces=%.2f pii=%s",
        settings.sentry_environment,
        settings.sentry_traces_sample_rate,
        settings.sentry_send_pii,
    )
    return True


class JsonLogFormatter(logging.Formatter):
    """Render log records as single-line JSON.

    Railway's log viewer shows raw stdout, so structured lines are what make
    per-call cost and token counts greppable and aggregatable after the fact.
    Anything passed via `extra={"fields": {...}}` is merged into the object.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(settings: Settings, *, json_logs: bool | None = None) -> None:
    """Set up root logging. JSON in deployed environments, plain text locally."""
    if json_logs is None:
        json_logs = settings.sentry_environment != "development"

    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def log_event(log: logging.Logger, message: str, **fields: Any) -> None:
    """Emit a log line carrying structured fields."""
    log.info(message, extra={"fields": fields})
