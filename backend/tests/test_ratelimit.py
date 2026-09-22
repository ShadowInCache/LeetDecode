"""Tests for rate limiting and the global spend cap.

The headline test is `test_minting_new_install_ids_stops_being_free` - that is
the vector the per-install quota cannot defend against on its own, and the whole
reason this module exists.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import ratelimit
from app.config import ProviderName, Settings
from app.llm.base import AllProvidersFailed, ProviderCallFailed
from app.llm.service import TranslationResult
from app.main import app
from app.models import RateLimitBucket
from app.routers import translate as translate_router
from app.schemas import SimplifiedProblem
from tests.test_schemas import VALID

client = TestClient(app)

NOW = datetime(2026, 9, 22, 14, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The limiter itself
# ---------------------------------------------------------------------------


class TestConsume:
    def test_allows_up_to_the_limit_then_refuses(self, session: Session) -> None:
        outcomes = [
            ratelimit.consume(
                session, key="k", limit=3, window_seconds=3600, now=NOW
            ).allowed
            for _ in range(5)
        ]
        assert outcomes == [True, True, True, False, False]

    def test_refusal_does_not_keep_incrementing(self, session: Session) -> None:
        """A client hammering a full bucket must not inflate its own count."""
        for _ in range(10):
            ratelimit.consume(session, key="k", limit=2, window_seconds=3600, now=NOW)

        assert ratelimit.current_usage(session, key="k", window_seconds=3600, now=NOW) == 2

    def test_counts_are_per_key(self, session: Session) -> None:
        assert ratelimit.consume(session, key="a", limit=1, window_seconds=3600, now=NOW).allowed
        assert not ratelimit.consume(session, key="a", limit=1, window_seconds=3600, now=NOW).allowed
        # A different key has its own allowance.
        assert ratelimit.consume(session, key="b", limit=1, window_seconds=3600, now=NOW).allowed

    def test_next_window_resets_the_count(self, session: Session) -> None:
        assert ratelimit.consume(session, key="k", limit=1, window_seconds=3600, now=NOW).allowed
        assert not ratelimit.consume(session, key="k", limit=1, window_seconds=3600, now=NOW).allowed

        later = NOW + timedelta(hours=1)
        assert ratelimit.consume(session, key="k", limit=1, window_seconds=3600, now=later).allowed

    def test_retry_after_points_at_the_window_end(self, session: Session) -> None:
        # NOW is 14:30; the hourly window ends at 15:00, i.e. 1800s away.
        result = ratelimit.consume(session, key="k", limit=1, window_seconds=3600, now=NOW)
        assert 1700 < result.retry_after_seconds <= 1800

    def test_interleaved_sessions_cannot_both_take_the_last_unit(
        self, engine, session: Session
    ) -> None:
        """The limit is enforced by the database, not by a Python comparison."""
        ratelimit.consume(session, key="k", limit=2, window_seconds=3600, now=NOW)

        with Session(engine) as a, Session(engine) as b:
            claimed_a = ratelimit.consume(a, key="k", limit=2, window_seconds=3600, now=NOW).allowed
            claimed_b = ratelimit.consume(b, key="k", limit=2, window_seconds=3600, now=NOW).allowed

        assert [claimed_a, claimed_b].count(True) == 1
        assert ratelimit.current_usage(session, key="k", window_seconds=3600, now=NOW) == 2

    def test_refund_returns_a_unit(self, session: Session) -> None:
        ratelimit.consume(session, key="k", limit=1, window_seconds=3600, now=NOW)
        assert not ratelimit.consume(session, key="k", limit=1, window_seconds=3600, now=NOW).allowed

        ratelimit.refund(session, key="k", window_seconds=3600, now=NOW)
        assert ratelimit.consume(session, key="k", limit=1, window_seconds=3600, now=NOW).allowed

    def test_refund_never_goes_negative(self, session: Session) -> None:
        for _ in range(3):
            ratelimit.refund(session, key="k", window_seconds=3600, now=NOW)
        assert ratelimit.current_usage(session, key="k", window_seconds=3600, now=NOW) == 0


class TestPurge:
    def test_removes_only_old_windows(self, session: Session) -> None:
        ratelimit.consume(session, key="old", limit=5, window_seconds=3600,
                          now=datetime.now(timezone.utc) - timedelta(days=5))
        ratelimit.consume(session, key="fresh", limit=5, window_seconds=3600)

        removed = ratelimit.purge_expired(session, older_than_seconds=2 * 86_400)

        assert removed == 1
        keys = [r.bucket_key for r in session.exec(select(RateLimitBucket)).all()]
        assert keys == ["fresh"]


# ---------------------------------------------------------------------------
# Enforcement through the endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_llm(monkeypatch):
    calls = {"n": 0}

    def _translate(_text, settings=None):
        calls["n"] += 1
        return TranslationResult(
            problem=SimplifiedProblem.model_validate(VALID),
            provider=ProviderName.GROQ,
        )

    monkeypatch.setattr(translate_router, "translate_problem", _translate)
    return calls


def body(n: int, install_id: str) -> dict:
    return {
        "install_id": install_id,
        "raw_text": (
            f"Problem Number {n}\n\nA distinct practice problem body, number {n}, "
            "long enough to pass request validation."
        ),
    }


@pytest.mark.usefixtures("engine")
class TestEndpointEnforcement:
    def test_minting_new_install_ids_stops_being_free(self, fake_llm) -> None:
        """The quota-bypass vector, closed.

        A fresh UUID buys a fresh five-call allowance, so without this limit one
        IP could loop forever. After a few registrations from the same address,
        new installs are refused.
        """
        statuses = []
        for i in range(6):
            install = f"minted-install-{i:04d}-aaaa-bbbb-cccc"
            statuses.append(client.post("/translate", json=body(i, install)).status_code)

        assert statuses[:3] == [200, 200, 200], "the first few installs are fine"
        assert statuses[3:] == [429, 429, 429], "then new registrations are refused"
        assert fake_llm["n"] == 3, "no LLM spend beyond the allowed registrations"

    def test_known_install_is_not_blocked_by_the_install_limit(self, fake_llm) -> None:
        """The new-install limit must not punish a returning user."""
        install = "returning-install-0001-aaaa-bbbb"
        assert client.post("/translate", json=body(0, install)).status_code == 200

        # Burn the new-install allowance with other IDs.
        for i in range(5):
            client.post("/translate", json=body(100 + i, f"other-{i:04d}-aaaa-bbbb-cc"))

        # The original install still works - it isn't a new registration.
        assert client.post("/translate", json=body(1, install)).status_code == 200

    def test_per_ip_llm_limit_refuses_with_429_and_retry_after(self, fake_llm):
        app.dependency_overrides[translate_router.get_settings] = lambda: Settings(
            rate_limit_llm_per_hour=2, rate_limit_new_installs_per_hour=99
        )
        try:
            install = "llm-limit-install-0001-aaaa-bb"
            codes = [client.post("/translate", json=body(i, install)).status_code for i in range(4)]
            assert codes == [200, 200, 429, 429]

            last = client.post("/translate", json=body(9, install))
            assert last.json()["detail"]["error"] == "RATE_LIMITED"
            assert int(last.headers["Retry-After"]) > 0
        finally:
            app.dependency_overrides.clear()

    def test_cache_hits_are_never_rate_limited_for_cost(self, fake_llm) -> None:
        """A cached problem must stay free even after the cost brake trips."""
        app.dependency_overrides[translate_router.get_settings] = lambda: Settings(
            rate_limit_llm_per_hour=1, rate_limit_new_installs_per_hour=99
        )
        try:
            install = "cache-free-install-0001-aaaa"
            assert client.post("/translate", json=body(0, install)).status_code == 200
            # Cost brake is now spent: a *new* problem is refused...
            assert client.post("/translate", json=body(1, install)).status_code == 429
            # ...but the cached one still works, repeatedly.
            for _ in range(3):
                again = client.post("/translate", json=body(0, install))
                assert again.status_code == 200
                assert again.json()["source"] == "cache"
        finally:
            app.dependency_overrides.clear()

    def test_global_cap_trips_into_cache_only_mode(self, fake_llm) -> None:
        app.dependency_overrides[translate_router.get_settings] = lambda: Settings(
            llm_daily_cap=2, rate_limit_new_installs_per_hour=99
        )
        try:
            install = "cap-install-0001-aaaa-bbbb-cc"
            assert client.post("/translate", json=body(0, install)).status_code == 200
            assert client.post("/translate", json=body(1, install)).status_code == 200

            over = client.post("/translate", json=body(2, install))
            assert over.status_code == 503
            assert over.json()["detail"]["error"] == "AT_CAPACITY"

            # Cache-only, not down: previously cached problems still answer.
            cached = client.post("/translate", json=body(0, install))
            assert cached.status_code == 200
            assert cached.json()["source"] == "cache"
        finally:
            app.dependency_overrides.clear()

    def test_failed_generation_refunds_the_global_cap(self, monkeypatch) -> None:
        """An outage must not eat the day's budget."""
        def blow_up(_t, settings=None):
            raise AllProvidersFailed({ProviderName.GEMINI: ProviderCallFailed("down")})

        monkeypatch.setattr(translate_router, "translate_problem", blow_up)
        app.dependency_overrides[translate_router.get_settings] = lambda: Settings(
            llm_daily_cap=2, rate_limit_new_installs_per_hour=99
        )
        try:
            install = "refund-install-0001-aaaa-bbbb"
            for i in range(2):
                assert client.post("/translate", json=body(i, install)).status_code == 502
        finally:
            app.dependency_overrides.clear()

        # The cap was reserved twice and refunded twice, so it is untouched.
        from app.db import get_engine

        with Session(get_engine()) as s:
            used = ratelimit.current_usage(
                s, key=ratelimit.GLOBAL_LLM_KEY, window_seconds=ratelimit.DAY_SECONDS
            )
        assert used == 0

    def test_limits_can_be_switched_off(self, fake_llm) -> None:
        app.dependency_overrides[translate_router.get_settings] = lambda: Settings(
            rate_limits_enabled=False
        )
        try:
            codes = [
                client.post("/translate", json=body(i, f"off-{i:04d}-aaaa-bbbb-cccc")).status_code
                for i in range(6)
            ]
            assert codes == [200] * 6
        finally:
            app.dependency_overrides.clear()
