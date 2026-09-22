"""Run the daily-problem job once, by hand.

    python scripts/run_daily_job.py

Useful for testing the job without waiting for the 24-hour timer. Hits the real
LeetCode GraphQL endpoint and, on a cache miss, spends one LLM call.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.daily import refresh_daily_problem  # noqa: E402
from app.db import create_db_and_tables  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
)

if __name__ == "__main__":
    create_db_and_tables()
    ok = refresh_daily_problem()
    print("daily problem cached" if ok else "daily job failed - see the log above")
    sys.exit(0 if ok else 1)
