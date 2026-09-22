"""SQLModel table definitions.

Postgres is the single source of truth: it holds both the translation cache and
the per-install usage counters. There is no Redis and no second store.

A note on the JSON column: `simplified_json` is declared as `JSON` with a
Postgres variant of `JSONB`. Production gets real JSONB (indexable, binary,
which is what the SRS specifies); SQLite - used by the test suite - gets plain
JSON, so the cache logic can be tested without a Postgres install.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Column, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel

# Real JSONB on Postgres, plain JSON elsewhere.
JSON_COLUMN = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    """Timezone-aware UTC now. Used as the default for every timestamp here."""
    return datetime.now(timezone.utc)


class ProblemCache(SQLModel, table=True):
    """A cached translation, keyed by a hash of the normalized problem text."""

    __tablename__ = "problems_cache"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # SHA-256 of the normalized (trimmed, whitespace-collapsed, lowercased)
    # raw text. Unique, so the same paste can never be stored twice.
    problem_hash: str = Field(sa_column=Column(String(64), unique=True, index=True, nullable=False))

    # First non-empty line of the paste. Indexed, and used for the fallback
    # lookup when the hash misses because the user's paste differs slightly.
    problem_title: str = Field(sa_column=Column(String(512), index=True, nullable=False))

    simplified_json: dict[str, Any] = Field(sa_column=Column(JSON_COLUMN, nullable=False))

    # True for the ~200-250 curated problems and the daily problem. These are
    # always free: a hit on any cached row costs no quota, but this flag lets us
    # tell curated coverage from organically-cached rows when reviewing usage.
    is_preseeded: bool = Field(default=False, index=True)

    created_at: datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    last_used_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )


class UsageLog(SQLModel, table=True):
    """Per-install quota counter, one row per anonymous install id."""

    __tablename__ = "usage_log"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    install_id: str = Field(
        sa_column=Column(String(128), unique=True, index=True, nullable=False)
    )

    # Logged for abuse-pattern review only. Per the SRS this never blocks a
    # request - shared office and campus IPs are normal and legitimate.
    ip_address: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))

    free_llm_calls_used: int = Field(default=0, nullable=False)

    last_request_at: datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class RateLimitBucket(SQLModel, table=True):
    """One fixed-window counter.

    Postgres-backed rather than in-process, for two reasons: an in-memory
    counter resets on every restart (and Railway restarts often), and it would
    not be shared if the service ever runs more than one instance. Keeping it
    here also honours the SRS constraint that Postgres is the single source of
    truth - no Redis.

    Rows are disposable: `purge_expired()` drops windows that have passed.
    """

    __tablename__ = "rate_limit_bucket"
    __table_args__ = (
        UniqueConstraint("bucket_key", "window_start", name="uq_bucket_key_window"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Namespaced identity, e.g. "llm:ip:203.0.113.7" or "llm:global".
    bucket_key: str = Field(sa_column=Column(String(200), index=True, nullable=False))

    # Start of the window this count belongs to, truncated to the window size.
    window_start: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )

    count: int = Field(default=0, nullable=False)


# Composite index supporting the fallback lookup: find a preseeded row by title.
Index(
    "ix_problems_cache_title_preseeded",
    ProblemCache.__table__.c.problem_title,
    ProblemCache.__table__.c.is_preseeded,
)
