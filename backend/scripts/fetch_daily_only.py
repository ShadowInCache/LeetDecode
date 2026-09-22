"""Fetch the daily problem and print it, without calling any LLM.

    python scripts/fetch_daily_only.py

Verifies the LeetCode GraphQL call and the HTML-to-text conversion in isolation,
with no API key and no cost.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.daily import DailyProblemError, fetch_daily_problem  # noqa: E402

if __name__ == "__main__":
    try:
        daily = fetch_daily_problem()
    except DailyProblemError as exc:
        print(f"failed: {exc}")
        sys.exit(1)

    print(f"date       : {daily.date}")
    print(f"title      : {daily.title}")
    print(f"slug       : {daily.slug}")
    print(f"difficulty : {daily.difficulty}")
    print("-" * 60)
    print(daily.as_raw_text()[:1500])
