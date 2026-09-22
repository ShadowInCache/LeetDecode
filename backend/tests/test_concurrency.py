"""Tests for the Phase 1 correctness fixes.

Each of these covers a window that only opens under concurrent load, so they
drive the race path deterministically rather than hoping to hit it by timing.
"""

import pytest
from apscheduler.triggers.cron import CronTrigger
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import cache as cache_module
from app import usage
from app.config import ProviderName, Settings
from app.llm.base import AllProvidersFailed, ProviderCallFailed
from app.llm.service import TranslationResult
from app.main import app
from app.models import ProblemCache, UsageLog
from app.routers import translate as translate_router
from app.schemas import SimplifiedProblem
from app.scheduler import DAILY_JOB_ID, shutdown_scheduler, start_scheduler
from tests.test_schemas import VALID

client = TestClient(app)

INSTALL = "race-test-install-0001"
TEXT = (
    "Two Sum\n\nGiven an array of integers nums and an integer target, return "
    "indices of the two numbers such that they add up to target."
)


@pytest.fixture
def problem() -> SimplifiedProblem:
    return SimplifiedProblem.model_validate(VALID)


# ---------------------------------------------------------------------------
# Fix 1 - the daily job must not use an interval trigger
# ---------------------------------------------------------------------------


class TestDailyJobTrigger:
    def test_uses_a_wall_clock_cron_trigger(self) -> None:
        """An interval trigger resets on every redeploy and may never fire."""
        settings = Settings(enable_scheduler=True, daily_job_hour=3)
        scheduler = start_scheduler(settings)
        try:
            assert scheduler is not None
            job = scheduler.get_job(DAILY_JOB_ID)
            assert isinstance(job.trigger, CronTrigger), (
                "daily job must be anchored to wall-clock time, not an interval"
            )
            assert "hour='3'" in str(job.trigger)
        finally:
            shutdown_scheduler()

    def test_disabled_scheduler_starts_nothing(self) -> None:
        assert start_scheduler(Settings(enable_scheduler=False)) is None


# ---------------------------------------------------------------------------
# Fix 2 - concurrent insert of the same problem
# ---------------------------------------------------------------------------


class TestConcurrentCacheInsert:
    def test_losing_writer_returns_the_stored_row(
        self, session: Session, problem, monkeypatch
    ) -> None:
        """Two requests cache the same problem; the loser must not 500."""
        winner = cache_module.store_translation(
            session, raw_text=TEXT, problem=problem
        )

        # Simulate the race: force the pre-insert SELECT to miss, so the second
        # call proceeds to INSERT against a hash that already exists.
        real_exec = session.exec
        calls = {"n": 0}

        class EmptyResult:
            def first(self):
                return None

        def flaky_exec(statement, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:  # only the initial existence check
                return EmptyResult()
            return real_exec(statement, *a, **kw)

        monkeypatch.setattr(session, "exec", flaky_exec)

        loser = cache_module.store_translation(session, raw_text=TEXT, problem=problem)

        assert loser.id == winner.id, "loser should resolve to the winning row"

        monkeypatch.undo()
        rows = session.exec(select(ProblemCache)).all()
        assert len(rows) == 1, "the race must not create a duplicate row"


# ---------------------------------------------------------------------------
# Fix 3 - atomic quota reservation
# ---------------------------------------------------------------------------


class TestQuotaReservation:
    def test_reserve_increments_and_reports_success(self, session: Session) -> None:
        settings = Settings(free_call_limit=2)
        usage.get_or_create(session, INSTALL)

        assert usage.try_reserve_call(session, INSTALL, settings) is True
        assert usage.try_reserve_call(session, INSTALL, settings) is True
        # Third exceeds the limit of 2.
        assert usage.try_reserve_call(session, INSTALL, settings) is False

        row = session.exec(select(UsageLog).where(UsageLog.install_id == INSTALL)).one()
        session.refresh(row)
        assert row.free_llm_calls_used == 2, "a refused reservation must not increment"

    def test_interleaved_sessions_cannot_both_claim_the_last_call(
        self, engine, session: Session
    ) -> None:
        """The real race, reproduced: two requests that both see room.

        This is the interleaving the old check-then-increment code got wrong.
        Both sessions read the counter while it still shows an unused call - as
        two concurrent requests would during a multi-second generation - and
        only then does either try to claim it. Exactly one must win.
        """
        settings = Settings(free_call_limit=1)
        usage.get_or_create(session, INSTALL)

        with Session(engine) as session_a, Session(engine) as session_b:
            # Both observe free_llm_calls_used == 0. Under a Python-side
            # `if used < limit` check, both would now proceed to call the LLM.
            row_a = session_a.exec(
                select(UsageLog).where(UsageLog.install_id == INSTALL)
            ).one()
            row_b = session_b.exec(
                select(UsageLog).where(UsageLog.install_id == INSTALL)
            ).one()
            assert row_a.free_llm_calls_used == 0
            assert row_b.free_llm_calls_used == 0

            # Now both try to claim. The conditional UPDATE decides.
            claimed_a = usage.try_reserve_call(session_a, INSTALL, settings)
            claimed_b = usage.try_reserve_call(session_b, INSTALL, settings)

        assert [claimed_a, claimed_b].count(True) == 1, (
            "exactly one of two interleaved requests may claim the last call"
        )

        row = session.exec(select(UsageLog).where(UsageLog.install_id == INSTALL)).one()
        session.refresh(row)
        assert row.free_llm_calls_used == 1, "the counter must not overshoot the limit"

    def test_refund_gives_the_call_back(self, session: Session) -> None:
        settings = Settings(free_call_limit=1)
        usage.get_or_create(session, INSTALL)

        assert usage.try_reserve_call(session, INSTALL, settings) is True
        assert usage.try_reserve_call(session, INSTALL, settings) is False

        usage.refund_call(session, INSTALL)
        assert usage.try_reserve_call(session, INSTALL, settings) is True

    def test_refund_never_goes_negative(self, session: Session) -> None:
        usage.get_or_create(session, INSTALL)
        for _ in range(3):
            usage.refund_call(session, INSTALL)

        row = session.exec(select(UsageLog).where(UsageLog.install_id == INSTALL)).one()
        session.refresh(row)
        assert row.free_llm_calls_used == 0


# ---------------------------------------------------------------------------
# The reserve/refund pair, end to end through the endpoint
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("engine")
class TestReserveRefundThroughTheAPI:
    def _body(self, n: int) -> dict:
        return {
            "install_id": INSTALL,
            "raw_text": (
                f"Problem Number {n}\n\nA distinct practice problem body, number "
                f"{n}, long enough to pass request validation."
            ),
        }

    def test_successful_call_consumes_exactly_one(self, monkeypatch) -> None:
        monkeypatch.setattr(
            translate_router,
            "translate_problem",
            lambda _t, settings=None: TranslationResult(
                problem=SimplifiedProblem.model_validate(VALID),
                provider=ProviderName.GROQ,
            ),
        )
        assert client.post("/translate", json=self._body(1)).status_code == 200
        assert client.get(f"/usage/{INSTALL}").json()["free_calls_used"] == 1

    def test_provider_failure_refunds_the_reservation(self, monkeypatch) -> None:
        def blow_up(_t, settings=None):
            raise AllProvidersFailed({ProviderName.GEMINI: ProviderCallFailed("down")})

        monkeypatch.setattr(translate_router, "translate_problem", blow_up)

        for n in range(4):
            assert client.post("/translate", json=self._body(n)).status_code == 502

        usage_body = client.get(f"/usage/{INSTALL}").json()
        assert usage_body["free_calls_used"] == 0, "our outage must not bill the user"
        assert usage_body["free_calls_remaining"] == 5

    def test_unexpected_error_also_refunds(self, monkeypatch) -> None:
        def explode(_t, settings=None):
            raise RuntimeError("something nobody predicted")

        monkeypatch.setattr(translate_router, "translate_problem", explode)

        with pytest.raises(RuntimeError):
            client.post("/translate", json=self._body(1))

        assert client.get(f"/usage/{INSTALL}").json()["free_calls_used"] == 0
