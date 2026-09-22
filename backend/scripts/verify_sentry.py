"""Send one test error to Sentry and confirm it was accepted.

    python scripts/verify_sentry.py

Deliberately a script rather than a `/sentry-debug` route: an always-on endpoint
that raises would be a live 500 generator that anyone could hit in production.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.observability import init_sentry  # noqa: E402


class LeetDecodeSentryTest(Exception):
    """Deliberate test exception - safe to ignore or resolve in Sentry."""


if __name__ == "__main__":
    settings = get_settings()

    if not init_sentry(settings):
        print("Sentry is not configured. Set SENTRY_DSN in backend/.env")
        sys.exit(1)

    import sentry_sdk

    event_id = sentry_sdk.capture_exception(
        LeetDecodeSentryTest("verification event from scripts/verify_sentry.py")
    )
    # Block until queued events are delivered, so the exit code is meaningful.
    sentry_sdk.flush(timeout=10)

    if event_id:
        print(f"sent event {event_id}")
        print(f"environment: {settings.sentry_environment}")
        print("Check your Sentry Issues page for 'LeetDecodeSentryTest'.")
        sys.exit(0)

    print("Sentry accepted no event - check the DSN.")
    sys.exit(1)
