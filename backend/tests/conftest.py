"""Shared test fixtures.

The database tests run against an in-memory SQLite database rather than
Postgres, so the suite needs no running server. The one Postgres-specific
detail - JSONB - is declared as a dialect variant in `app.models`, so the same
table definitions work on both.
"""

import os

# Must happen before anything imports `app.config`: a real DSN in .env would
# otherwise have the test suite reporting its deliberate failures to the live
# Sentry project. An env var outranks the .env file in pydantic-settings.
os.environ["SENTRY_DSN"] = ""

import pytest  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from app import db  # noqa: E402

# Importing the models registers them on SQLModel.metadata. Without it, a test
# module that never imports them would get an engine with zero tables - which
# passes in a full-suite run (something else imported them) and fails when that
# file is run on its own.
import app.models  # noqa: E402,F401


@pytest.fixture
def engine():
    """A fresh in-memory database per test."""
    # StaticPool + a shared connection keeps every session pointed at the same
    # in-memory database; without it each connection gets its own empty one.
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)
    db.set_engine(test_engine)
    try:
        yield test_engine
    finally:
        db.set_engine(None)
        SQLModel.metadata.drop_all(test_engine)
        test_engine.dispose()


@pytest.fixture
def session(engine) -> Session:
    with Session(engine) as s:
        yield s
