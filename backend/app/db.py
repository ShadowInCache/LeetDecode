"""Database engine, session management and schema creation."""

import logging
from collections.abc import Generator

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_engine: Engine | None = None


def _normalize_url(url: str) -> str:
    """Make a platform-provided URL usable by SQLAlchemy + psycopg2.

    Railway and Heroku hand out `postgres://...`, a scheme SQLAlchemy dropped
    support for. Rewriting it here means the deploy works with the injected
    DATABASE_URL untouched.
    """
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def get_engine(settings: Settings | None = None) -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is not None:
        return _engine

    settings = settings or get_settings()
    if not settings.database_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Point it at a Postgres instance - see .env.example."
        )

    url = _normalize_url(settings.database_url)
    # pool_pre_ping avoids handing out connections that a managed Postgres has
    # already closed behind our back, which is the usual cause of the first
    # request after an idle period failing on platforms like Railway.
    _engine = create_engine(url, pool_pre_ping=True, echo=False)
    logger.info("database engine created dialect=%s", _engine.dialect.name)
    return _engine


def set_engine(engine: Engine | None) -> None:
    """Override the process-wide engine. Used by the test suite."""
    global _engine
    _engine = engine


def create_db_and_tables(engine: Engine | None = None) -> None:
    """Create any missing tables.

    Adequate for this project: append-mostly tables and no destructive
    migrations. If the schema starts changing shape, bring in Alembic.
    """
    # Importing the models is what registers them on SQLModel.metadata. Without
    # this the call silently creates nothing whenever the caller has not already
    # imported them - which is every path except the running app, where the
    # routers happen to import them first.
    import app.models  # noqa: F401

    SQLModel.metadata.create_all(engine or get_engine())
    logger.info("database tables ensured")


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a session per request."""
    with Session(get_engine()) as session:
        yield session
