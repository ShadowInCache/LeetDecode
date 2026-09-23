"""Configuration checks that run at startup and on demand.

The failure this exists to prevent: a deploy where the app boots fine, `/health`
answers, and then *every translation* fails with `ProviderNotConfigured` because
a variable never reached the service. Nothing looks broken until a user tries to
use it.

So startup logs an explicit summary of what is and isn't configured, and
`scripts/preflight.py` gives the same answer as a command with a meaningful exit
code.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy import text

from app.config import ProviderName, Settings

logger = logging.getLogger(__name__)


class Level(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class Check:
    name: str
    level: Level
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, level: Level, detail: str) -> None:
        self.checks.append(Check(name, level, detail))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.level is Level.FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.level is Level.WARN]

    @property
    def ok(self) -> bool:
        return not self.failures


def _check_providers(settings: Settings, report: Report) -> None:
    """At least one provider in the chain must have a key."""
    chain = [settings.llm_provider]
    if settings.llm_fallback_provider and settings.llm_fallback_provider != settings.llm_provider:
        chain.append(settings.llm_fallback_provider)

    configured = [p for p in chain if settings.api_key_for(p)]
    missing = [p for p in chain if not settings.api_key_for(p)]

    env_name = {
        ProviderName.GEMINI: "GEMINI_API_KEY",
        ProviderName.GROQ: "GROQ_API_KEY",
    }

    if not configured:
        report.add(
            "llm providers",
            Level.FAIL,
            "no API key for any provider in the chain "
            f"({', '.join(env_name[p] for p in chain)}) - every translation will fail. "
            "On Railway, check the variables are *shared into the service*, not just "
            "defined as environment-level Shared Variables.",
        )
        return

    detail = f"{', '.join(p.value for p in configured)} configured"
    if missing:
        report.add(
            "llm providers",
            Level.WARN,
            f"{detail}; no key for {', '.join(env_name[p] for p in missing)} "
            "- failover is unavailable",
        )
    else:
        report.add("llm providers", Level.OK, f"{detail} (primary + fallback)")

    for provider in configured:
        model = settings.model_for(provider)
        from app.pricing import PRICES

        if model not in PRICES:
            report.add(
                f"pricing/{provider.value}",
                Level.WARN,
                f"no price on file for {model!r} - cost will log as unknown",
            )


def _check_database(settings: Settings, report: Report) -> None:
    if not settings.database_url:
        report.add(
            "database",
            Level.FAIL,
            "DATABASE_URL is not set. On Railway, attach a Postgres database and it "
            "is injected automatically.",
        )
        return

    try:
        from app.db import get_engine
    except ImportError as exc:
        # Not a connectivity problem at all - almost always the wrong
        # interpreter, i.e. the system Python instead of the venv.
        report.add(
            "dependencies",
            Level.FAIL,
            f"{exc}. Run with the virtualenv interpreter: "
            r".venv\Scripts\python.exe scripts/preflight.py",
        )
        return

    try:
        engine = get_engine(settings)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - the message is the point
        hint = ""
        message = str(exc)
        if "could not translate host name" in message and ".supabase.co" in message:
            hint = (
                " | This is Supabase's DIRECT endpoint, which is IPv6-only on the "
                "free tier. Use the SESSION POOLER string instead: host ends in "
                ".pooler.supabase.com and the user is postgres.<project-ref>."
            )
        elif "password authentication failed" in message:
            hint = " | Check the password, and URL-encode any @ : / ? # characters."
        report.add("database", Level.FAIL, f"cannot connect: {exc}{hint}")
        return

    report.add("database", Level.OK, f"reachable ({engine.dialect.name})")

    # Tables are created on startup, so a missing one means startup failed.
    try:
        # Importing the models module is what registers the tables on
        # SQLModel.metadata; without it this check silently compares nothing.
        import app.models  # noqa: F401
        from sqlmodel import SQLModel

        expected = set(SQLModel.metadata.tables)
        if not expected:
            report.add("schema", Level.WARN, "no tables registered on metadata")
            return
        from sqlalchemy import inspect

        present = set(inspect(engine).get_table_names())
        missing = expected - present
        if missing:
            report.add(
                "schema", Level.WARN, f"missing tables: {', '.join(sorted(missing))}"
            )
        else:
            report.add("schema", Level.OK, f"{len(expected)} tables present")
    except Exception as exc:  # noqa: BLE001
        report.add("schema", Level.WARN, f"could not inspect: {exc}")


def _check_operational(settings: Settings, report: Report) -> None:
    is_production = settings.sentry_environment != "development"

    if settings.admin_token:
        report.add("admin dashboard", Level.OK, "enabled at /admin")
    else:
        report.add(
            "admin dashboard",
            Level.WARN,
            "ADMIN_TOKEN is empty - /admin is disabled (404)",
        )

    if settings.sentry_dsn:
        report.add(
            "sentry", Level.OK, f"enabled, environment={settings.sentry_environment}"
        )
    else:
        report.add("sentry", Level.WARN, "SENTRY_DSN empty - error tracking disabled")

    if is_production and not settings.rate_limits_enabled:
        report.add(
            "rate limits",
            Level.FAIL,
            "RATE_LIMITS_ENABLED is false outside development - /translate would be "
            "an open, unmetered door to a paid API",
        )
    elif settings.rate_limits_enabled:
        report.add(
            "rate limits",
            Level.OK,
            f"on - {settings.rate_limit_llm_per_hour}/h per IP, "
            f"{settings.rate_limit_new_installs_per_hour} new installs/h, "
            f"{settings.llm_daily_cap}/day global",
        )
    else:
        report.add("rate limits", Level.WARN, "disabled (development)")

    if is_production and settings.sentry_send_pii:
        report.add(
            "privacy",
            Level.WARN,
            "SENTRY_SEND_PII is on - client IPs and headers go to Sentry, which your "
            "privacy policy must disclose",
        )


def run(settings: Settings, *, include_database: bool = True) -> Report:
    """Run every check and return the report."""
    report = Report()
    _check_providers(settings, report)
    if include_database:
        _check_database(settings, report)
    _check_operational(settings, report)
    return report


def log_report(report: Report) -> None:
    """Emit the report through the normal logger, one line per check."""
    for check in report.checks:
        message = "preflight %-18s %s"
        if check.level is Level.FAIL:
            logger.error(message, check.name, check.detail)
        elif check.level is Level.WARN:
            logger.warning(message, check.name, check.detail)
        else:
            logger.info(message, check.name, check.detail)

    if report.failures:
        logger.error(
            "preflight: %d check(s) FAILED - the service will accept requests but "
            "translations will not work",
            len(report.failures),
        )
