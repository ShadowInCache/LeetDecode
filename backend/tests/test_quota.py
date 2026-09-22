"""Quota enforcement and the /usage endpoint.

The behaviours worth guarding, in order of how much they'd hurt if broken:

  * a cache hit never costs quota (otherwise the preseeded set isn't free);
  * a failed generation never costs quota (otherwise our outage bills the user);
  * the 6th miss is refused with 403 QUOTA_EXCEEDED.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import ProviderName
from app.llm.base import AllProvidersFailed, ProviderCallFailed
from app.llm.service import TranslationResult
from app.main import app
from app.routers import translate as translate_router
from app.schemas import SimplifiedProblem
from tests.test_schemas import VALID

client = TestClient(app)
pytestmark = pytest.mark.usefixtures("engine")

INSTALL_ID = "3f2b1c9a-7d4e-4f1a-9c2b-8e5d6a7b3c1d"


def problem_text(n: int) -> str:
    return (
        f"Problem Number {n}\n\n"
        f"This is the body of a distinct practice problem, number {n}, long "
        "enough to satisfy the minimum length requirement for a request."
    )


def request_body(n: int, install_id: str = INSTALL_ID) -> dict:
    return {"install_id": install_id, "raw_text": problem_text(n)}


@pytest.fixture
def fake_llm(monkeypatch):
    """Patch the LLM with a counter; returns the counter dict."""
    calls = {"n": 0}

    def _translate(_text, settings=None):
        calls["n"] += 1
        return TranslationResult(
            problem=SimplifiedProblem.model_validate(VALID),
            provider=ProviderName.GEMINI,
        )

    monkeypatch.setattr(translate_router, "translate_problem", _translate)
    return calls


class TestQuotaEnforcement:
    def test_five_misses_succeed_and_the_sixth_is_refused(self, fake_llm) -> None:
        for n in range(5):
            response = client.post("/translate", json=request_body(n))
            assert response.status_code == 200, f"call {n} should succeed"
            assert response.json()["source"] == "llm"

        sixth = client.post("/translate", json=request_body(99))
        assert sixth.status_code == 403
        assert sixth.json()["detail"]["error"] == "QUOTA_EXCEEDED"
        # The LLM was never asked for the sixth.
        assert fake_llm["n"] == 5

    def test_cache_hits_are_free_and_unlimited(self, fake_llm) -> None:
        """Exhaust the quota, then confirm cached problems still work."""
        for n in range(5):
            client.post("/translate", json=request_body(n))
        assert client.post("/translate", json=request_body(99)).status_code == 403

        # Problem 0 is cached from the loop above; it must still be served.
        for _ in range(3):
            again = client.post("/translate", json=request_body(0))
            assert again.status_code == 200
            assert again.json()["source"] == "cache"

        assert fake_llm["n"] == 5

    def test_failed_generation_does_not_spend_quota(self, monkeypatch) -> None:
        """Our outage must not cost the user one of their five calls."""

        def blow_up(_text, settings=None):
            raise AllProvidersFailed({ProviderName.GEMINI: ProviderCallFailed("down")})

        monkeypatch.setattr(translate_router, "translate_problem", blow_up)

        for n in range(3):
            assert client.post("/translate", json=request_body(n)).status_code == 502

        usage = client.get(f"/usage/{INSTALL_ID}").json()
        assert usage["free_calls_used"] == 0
        assert usage["free_calls_remaining"] == 5

    def test_quota_is_per_install(self, fake_llm) -> None:
        other = "99998888-7777-6666-5555-444433332222"
        for n in range(5):
            client.post("/translate", json=request_body(n))
        assert client.post("/translate", json=request_body(50)).status_code == 403

        # A different install still has its own full allowance.
        assert client.post("/translate", json=request_body(50, other)).status_code == 200


class TestUsageEndpoint:
    def test_unknown_install_has_full_allowance(self) -> None:
        body = client.get("/usage/brand-new-install-id-000").json()
        assert body == {"free_calls_used": 0, "free_calls_remaining": 5}

    def test_counts_track_translations(self, fake_llm) -> None:
        client.post("/translate", json=request_body(1))
        client.post("/translate", json=request_body(2))

        body = client.get(f"/usage/{INSTALL_ID}").json()
        assert body == {"free_calls_used": 2, "free_calls_remaining": 3}

    def test_remaining_never_goes_negative(self, fake_llm, session) -> None:
        from sqlmodel import select

        from app.models import UsageLog

        client.post("/translate", json=request_body(1))
        row = session.exec(select(UsageLog).where(UsageLog.install_id == INSTALL_ID)).one()
        row.free_llm_calls_used = 99
        session.add(row)
        session.commit()

        assert client.get(f"/usage/{INSTALL_ID}").json()["free_calls_remaining"] == 0


def test_ip_address_is_recorded_but_never_blocks(fake_llm, session) -> None:
    from sqlmodel import select

    from app.models import UsageLog

    response = client.post(
        "/translate",
        json=request_body(1),
        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
    )
    assert response.status_code == 200

    row = session.exec(select(UsageLog).where(UsageLog.install_id == INSTALL_ID)).one()
    assert row.ip_address == "203.0.113.7"

    # A second install from the same IP is not blocked.
    shared = client.post(
        "/translate",
        json=request_body(2, "aaaabbbb-cccc-dddd-eeee-ffff00001111"),
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    assert shared.status_code == 200
