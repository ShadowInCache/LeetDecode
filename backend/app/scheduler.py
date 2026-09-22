"""In-process APScheduler wiring for the daily-problem job.

In-process rather than external cron because the SRS calls for one deployable
unit. The tradeoff to know about: if you scale past one web instance, every
instance runs its own copy of the job. That is *safe* here - the job checks the
cache before calling the LLM, so duplicates cost a query, not a generation - but
if you do scale out, set ENABLE_SCHEDULER=false on all but one instance.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Settings
from app.daily import refresh_daily_problem

logger = logging.getLogger(__name__)

DAILY_JOB_ID = "refresh_daily_problem"

_scheduler: BackgroundScheduler | None = None


def start_scheduler(settings: Settings) -> BackgroundScheduler | None:
    """Start the background scheduler, unless disabled by config."""
    global _scheduler

    if not settings.enable_scheduler:
        logger.info("scheduler disabled via ENABLE_SCHEDULER")
        return None
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        refresh_daily_problem,
        trigger=IntervalTrigger(hours=24),
        id=DAILY_JOB_ID,
        name="Fetch and pre-cache the LeetCode daily problem",
        # If the process was asleep past a fire time, run once on wake rather
        # than replaying every window we missed.
        coalesce=True,
        max_instances=1,
        # Give the web server a minute to come up before doing network work.
        misfire_grace_time=3600,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("scheduler started with job %r (every 24h)", DAILY_JOB_ID)
    return scheduler


def shutdown_scheduler() -> None:
    """Stop the scheduler if it is running."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("scheduler stopped")
