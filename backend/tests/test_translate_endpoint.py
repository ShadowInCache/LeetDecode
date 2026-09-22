"""HTTP-level tests for POST /translate."""

import json

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


# Every /translate call now goes through the cache, so these need a database.
# The `engine` fixture (tests/conftest.py) points `app.db` at in-memory SQLite.
pytestmark = pytest.mark.usefixtures("engine")

GOOD_REQUEST = {
    "install_id": "3f2b1c9a-7d4e-4f1a-9c2b-8e5d6a7b3c1d",
    "raw_text": (
        "Two Sum\n\nGiven an array of integers nums and an integer target, "
        "return indices of the two numbers such that they add up to target."
    ),
}


def test_health_still_ok() -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_successful_translation(monkeypatch) -> None:
    monkeypatch.setattr(
        translate_router,
        "translate_problem",
        lambda _text, settings=None: TranslationResult(
            problem=SimplifiedProblem.model_validate(VALID),
            provider=ProviderName.GEMINI,
        ),
    )
    response = client.post("/translate", json=GOOD_REQUEST)

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "llm"
    assert body["data"]["what_you_need_to_do"].startswith("Find two numbers")
    assert body["data"]["example"]["output"] == "[0, 1]"


def test_provider_failure_returns_502_not_a_partial_result(monkeypatch) -> None:
    def blow_up(_text, settings=None):
        raise AllProvidersFailed({ProviderName.GEMINI: ProviderCallFailed("down")})

    monkeypatch.setattr(translate_router, "translate_problem", blow_up)
    response = client.post("/translate", json=GOOD_REQUEST)

    assert response.status_code == 502
    assert response.json()["detail"]["error"] == "TRANSLATION_FAILED"


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("missing install_id", {"raw_text": "x" * 40}),
        ("missing raw_text", {"install_id": "a" * 36}),
        ("raw_text too short", {"install_id": "a" * 36, "raw_text": "Two Sum"}),
        ("unknown field", {**GOOD_REQUEST, "locale": "en"}),
    ],
)
def test_bad_requests_are_422(label: str, body: dict) -> None:
    assert client.post("/translate", json=body).status_code == 422


def test_second_identical_request_is_served_from_cache(monkeypatch) -> None:
    """The whole point of the cache: the LLM must be called exactly once."""
    calls = {"n": 0}

    def counting_translate(_text, settings=None):
        calls["n"] += 1
        return TranslationResult(
            problem=SimplifiedProblem.model_validate(VALID),
            provider=ProviderName.GEMINI,
        )

    monkeypatch.setattr(translate_router, "translate_problem", counting_translate)

    first = client.post("/translate", json=GOOD_REQUEST)
    second = client.post("/translate", json=GOOD_REQUEST)

    assert first.json()["source"] == "llm"
    assert second.json()["source"] == "cache"
    assert second.json()["data"] == first.json()["data"]
    assert calls["n"] == 1
