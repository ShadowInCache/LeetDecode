"""Check that this environment is configured correctly.

    python scripts/preflight.py           # local, reads backend/.env
    railway run python scripts/preflight.py   # against the deployed environment

Exit code is 0 when nothing failed, 1 otherwise, so it can gate a deploy.
Makes no LLM calls and costs nothing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.preflight import Level, run  # noqa: E402

SYMBOLS = {Level.OK: "  ok  ", Level.WARN: " warn ", Level.FAIL: " FAIL "}


def main() -> int:
    settings = get_settings()
    report = run(settings)

    print(f"LeetDecode preflight - environment: {settings.sentry_environment}\n")
    for check in report.checks:
        print(f"[{SYMBOLS[check.level]}] {check.name:18} {check.detail}")

    print()
    if report.failures:
        print(
            f"{len(report.failures)} check(s) failed. "
            "The service would start but translations would not work."
        )
        return 1

    if report.warnings:
        print(f"All required checks passed, with {len(report.warnings)} warning(s).")
    else:
        print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
