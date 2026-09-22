"""Shared test fixtures.

The database tests run against an in-memory SQLite database rather than
Postgres, so the suite needs no running server. The one Postgres-specific
detail - JSONB - is declared as a dialect variant in `app.models`, so the same
table definitions work on both.
"""

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import db


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
