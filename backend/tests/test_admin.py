"""Tests for the admin dashboard and its auth.

The security property that matters most: an unset ADMIN_TOKEN must *close* the
dashboard, not open it. A misconfiguration that silently exposes cost and
install data would be worse than one that 404s.
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import call_log, stats
from app.config import ProviderName, Settings
from app.llm.service import TranslationResult
from app.main import app
from app.routers import admin as admin_router
from app.routers import translate as translate_router
from app.schemas import SimplifiedProblem
from tests.test_schemas import VALID

client = TestClient(app)

TOKEN = "test-admin-token-abcdef123456"


@pytest.fixture
def admin_settings():
    """Enable the dashboard with a known token."""
    settings = Settings(admin_token=TOKEN)
    app.dependency_overrides[admin_router.get_settings] = lambda: settings
    app.dependency_overrides[translate_router.get_settings] = lambda: settings
    yield settings
    app.dependency_overrides.clear()


def auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.usefixtures("engine")
class TestAuth:
    def test_unset_token_closes_the_dashboard(self) -> None:
        """Not configured must mean closed, never open."""
        app.dependency_overrides[admin_router.get_settings] = lambda: Settings(
            admin_token=""
        )
        try:
            assert client.get("/admin").status_code == 404
            assert client.get("/admin/stats").status_code == 404
            # Even with a token supplied, there is nothing to unlock.
            assert client.get("/admin/stats", headers=auth()).status_code == 404
        finally:
            app.dependency_overrides.clear()

    def test_stats_requires_a_token(self, admin_settings) -> None:
        assert client.get("/admin/stats").status_code == 401

    def test_wrong_token_is_rejected(self, admin_settings) -> None:
        assert client.get("/admin/stats", headers=auth("nope")).status_code == 401

    def test_malformed_header_is_rejected(self, admin_settings) -> None:
        for header in ({"Authorization": TOKEN}, {"Authorization": "Basic " + TOKEN}):
            assert client.get("/admin/stats", headers=header).status_code == 401

    def test_correct_token_is_accepted(self, admin_settings) -> None:
        assert client.get("/admin/stats", headers=auth()).status_code == 200

    def test_page_itself_carries_no_data(self, admin_settings) -> None:
        """The HTML is unauthenticated, so it must contain no figures."""
        body = client.get("/admin").text
        assert "<!DOCTYPE html>" in body
        assert TOKEN not in body
        assert "/admin/stats" in body  # it fetches them instead


@pytest.mark.usefixtures("engine")
class TestStats:
    def test_empty_database_returns_zeros(self, admin_settings) -> None:
        body = client.get("/admin/stats", headers=auth()).json()

        assert body["today"]["llm_calls"] == 0
        assert body["today"]["cost_usd"] == 0
        assert body["today"]["cache_hit_rate"] is None
        assert body["cache"]["total"] == 0
        assert len(body["series"]) == 14

    def test_window_is_clamped(self, admin_settings) -> None:
        assert len(client.get("/admin/stats?days=1", headers=auth()).json()["series"]) == 1
        # 500 is clamped to the 90-day maximum.
        assert len(client.get("/admin/stats?days=500", headers=auth()).json()["series"]) == 90

    def test_counts_calls_and_cost(self, admin_settings, session: Session) -> None:
        for _ in range(3):
            call_log.record(
                session,
                provider=ProviderName.GROQ,
                model="openai/gpt-oss-120b",
                source=call_log.SOURCE_TRANSLATE,
                install_id="install-a",
                input_tokens=600,
                output_tokens=400,
                latency_ms=1500,
            )

        body = client.get("/admin/stats", headers=auth()).json()

        assert body["today"]["llm_calls"] == 3
        assert body["today"]["cost_usd"] > 0
        assert body["providers"][0]["provider"] == "groq"
        assert body["providers"][0]["calls"] == 3
        assert body["top_installs"][0]["install_id"] == "install-a"

    def test_failed_calls_are_counted_separately(self, admin_settings, session) -> None:
        call_log.record(
            session, provider=ProviderName.GEMINI, model="gemini-3.1-flash-lite",
            source=call_log.SOURCE_TRANSLATE, input_tokens=10, output_tokens=10,
            succeeded=False,
        )
        body = client.get("/admin/stats", headers=auth()).json()

        assert body["period"]["failures"] == 1
        # A failure isn't billable output, so it stays out of the call total.
        assert body["today"]["llm_calls"] == 0

    def test_cache_hit_rate_reflects_real_traffic(self, admin_settings, monkeypatch) -> None:
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
        body = {
            "install_id": "hit-rate-install-0001-aa",
            "raw_text": "Two Sum\n\nGiven an array of integers, return two indices.",
        }
        client.post("/translate", json=body)          # miss
        for _ in range(3):
            client.post("/translate", json=body)      # hits

        today = client.get("/admin/stats", headers=auth()).json()["today"]
        assert today["requests"] == 4
        assert today["cache_hits"] == 3
        assert today["cache_hit_rate"] == 0.75
        assert today["llm_calls"] == 1

    def test_cap_usage_is_reported(self, admin_settings, monkeypatch) -> None:
        monkeypatch.setattr(
            translate_router,
            "translate_problem",
            lambda _t, settings=None: TranslationResult(
                problem=SimplifiedProblem.model_validate(VALID),
                provider=ProviderName.GROQ,
                model="openai/gpt-oss-120b",
            ),
        )
        client.post(
            "/translate",
            json={
                "install_id": "cap-report-install-0001",
                "raw_text": "Valid Parentheses\n\nGiven a string of brackets, say if valid.",
            },
        )
        today = client.get("/admin/stats", headers=auth()).json()["today"]
        assert today["cap_used"] == 1
        assert today["cap_limit"] == Settings().llm_daily_cap


@pytest.mark.usefixtures("engine")
class TestCounters:
    def test_counters_are_isolated_per_key(self, session: Session) -> None:
        stats.record_request(session)
        stats.record_request(session)
        stats.record_cache_hit(session)

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        assert stats._daily_counter(session, stats.REQUESTS_KEY, now) == 2
        assert stats._daily_counter(session, stats.CACHE_HITS_KEY, now) == 1
