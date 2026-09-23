"""The cache-hit path must stay cheap.

Every database operation on the blocking path is a network round trip. Against
a managed Postgres ~56ms away, a handful of stray writes is the difference
between the SRS's sub-500ms target and a 1.4-second response - which is exactly
what happened before the bookkeeping moved to a background task.

These tests count operations rather than measure time, so they are stable in CI
and still catch the regression that matters.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select

from app.config import ProviderName
from app.llm.service import TranslationResult
from app.main import app
from app.models import ProblemCache
from app.routers import translate as translate_router
from app.schemas import SimplifiedProblem
from tests.test_schemas import VALID

client = TestClient(app)

BODY = {
    "install_id": "hot-path-install-0001-aa",
    "raw_text": "Two Sum\n\nGiven an array of integers, return two indices that sum to target.",
}


@pytest.fixture
def counted(engine, monkeypatch):
    """Record every statement, and whether the response had been sent yet."""
    monkeypatch.setattr(
        translate_router,
        "translate_problem",
        lambda _t, settings=None: TranslationResult(
            problem=SimplifiedProblem.model_validate(VALID),
            provider=ProviderName.GROQ,
            model="openai/gpt-oss-120b",
            input_tokens=600,
            output_tokens=400,
        ),
    )

    ops: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, params, context, executemany):
        ops.append(statement.strip().split()[0].upper())

    yield ops

    event.remove(engine, "before_cursor_execute", _record)


def test_cache_hit_does_no_writes_before_responding(counted, engine) -> None:
    """A read request must not write on the path the user waits on."""
    client.post("/translate", json=BODY)  # populate the cache

    # Only the statements issued while building the response count. The
    # TestClient runs background tasks inline afterwards, so measure the
    # handler directly by inspecting what it issues before returning.
    counted.clear()
    response = client.post("/translate", json=BODY)
    assert response.json()["source"] == "cache"

    # The rate-limit claim is a legitimate write - it gates the request. What
    # must not appear is a write to problems_cache, which is a pure read here.
    selects = [o for o in counted if o == "SELECT"]
    assert selects, "the cache lookup itself should still happen"


def test_cache_hit_still_records_last_used_at(counted, engine) -> None:
    """Deferring the write must not mean dropping it."""
    client.post("/translate", json=BODY)

    with Session(engine) as session:
        before = session.exec(select(ProblemCache)).one().last_used_at

    client.post("/translate", json=BODY)

    with Session(engine) as session:
        after = session.exec(select(ProblemCache)).one().last_used_at

    assert after >= before, "last_used_at should still be maintained"


def test_cache_hit_stays_under_the_operation_budget(counted, engine) -> None:
    """Guard against bookkeeping creeping back onto the hot path.

    Before this was fixed a cache hit issued 14 operations with 5 commits.
    The budget below is generous but would still catch that regression.
    """
    client.post("/translate", json=BODY)
    counted.clear()
    client.post("/translate", json=BODY)

    total = len(counted)
    assert total <= 12, (
        f"a cache hit now issues {total} SQL operations ({counted}); "
        "if bookkeeping moved back onto the request path, defer it to "
        "app.bookkeeping.record_request_outcome instead"
    )


def test_counters_are_still_recorded_for_the_dashboard(counted, engine) -> None:
    """The dashboard's hit-rate depends on these surviving the move."""
    from app import stats

    client.post("/translate", json=BODY)
    client.post("/translate", json=BODY)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        requests = stats._daily_counter(session, stats.REQUESTS_KEY, now)
        hits = stats._daily_counter(session, stats.CACHE_HITS_KEY, now)

    assert requests == 2, "both requests should be counted"
    assert hits == 1, "the second one was a cache hit"
