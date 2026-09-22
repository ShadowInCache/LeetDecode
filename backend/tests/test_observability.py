"""Tests for cost estimation, call logging and Sentry scrubbing."""

import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import call_log
from app.config import ProviderName, Settings
from app.llm.service import TranslationResult
from app.main import app
from app.models import LLMCallLog
from app.observability import JsonLogFormatter, _scrub_event, init_sentry, log_event
from app.pricing import PRICES, estimate_cost_usd
from app.routers import translate as translate_router
from app.schemas import SimplifiedProblem
from tests.test_schemas import VALID

client = TestClient(app)


class TestPricing:
    def test_known_model_gives_a_cost(self) -> None:
        # 1M in + 1M out on gpt-oss-120b = $0.15 + $0.60
        cost = estimate_cost_usd("openai/gpt-oss-120b", 1_000_000, 1_000_000)
        assert cost == pytest.approx(0.75)

    def test_realistic_translation_is_fractions_of_a_cent(self) -> None:
        cost = estimate_cost_usd("gemini-3.1-flash-lite", 600, 400)
        assert cost == pytest.approx(600 / 1e6 * 0.25 + 400 / 1e6 * 1.50)
        assert cost < 0.001

    def test_unknown_model_returns_none_rather_than_guessing(self) -> None:
        assert estimate_cost_usd("some-model-we-have-never-priced", 100, 100) is None

    def test_missing_token_counts_return_none(self) -> None:
        assert estimate_cost_usd("openai/gpt-oss-120b", None, 50) is None
        assert estimate_cost_usd("openai/gpt-oss-120b", 50, None) is None

    def test_configured_defaults_are_priced(self) -> None:
        """The models we ship with must have prices, or the dashboard is blind."""
        settings = Settings()
        assert settings.gemini_model in PRICES
        assert settings.groq_model in PRICES


class TestCallLog:
    def test_persists_a_row_with_a_cost(self, session: Session) -> None:
        cost = call_log.record(
            session,
            provider=ProviderName.GROQ,
            model="openai/gpt-oss-120b",
            source=call_log.SOURCE_TRANSLATE,
            install_id="install-0001",
            input_tokens=600,
            output_tokens=400,
            latency_ms=1500,
        )

        row = session.exec(select(LLMCallLog)).one()
        assert row.provider == "groq"
        assert row.input_tokens == 600
        assert row.cost_usd == pytest.approx(cost)
        assert row.succeeded is True

    def test_unknown_model_logs_tokens_without_a_cost(self, session: Session) -> None:
        call_log.record(
            session,
            provider=ProviderName.GEMINI,
            model="unpriced-model",
            source=call_log.SOURCE_DAILY_JOB,
            input_tokens=10,
            output_tokens=20,
        )
        row = session.exec(select(LLMCallLog)).one()
        assert row.cost_usd is None
        assert row.input_tokens == 10

    def test_works_without_a_session(self) -> None:
        """Call sites with no database handle still get a cost estimate."""
        cost = call_log.record(
            None,
            provider=ProviderName.GROQ,
            model="openai/gpt-oss-20b",
            source=call_log.SOURCE_PRESEED,
            input_tokens=1000,
            output_tokens=1000,
        )
        assert cost == pytest.approx(0.075 / 1000 + 0.30 / 1000)


@pytest.mark.usefixtures("engine")
class TestCallLoggedThroughTheEndpoint:
    def test_translation_records_one_row(self, monkeypatch) -> None:
        monkeypatch.setattr(
            translate_router,
            "translate_problem",
            lambda _t, settings=None: TranslationResult(
                problem=SimplifiedProblem.model_validate(VALID),
                provider=ProviderName.GROQ,
                model="openai/gpt-oss-120b",
                input_tokens=620,
                output_tokens=380,
                latency_ms=1600,
            ),
        )
        body = {
            "install_id": "logged-install-0001-aaaa",
            "raw_text": "Two Sum\n\nGiven an array of integers, return two indices.",
        }
        assert client.post("/translate", json=body).status_code == 200

        from app.db import get_engine

        with Session(get_engine()) as s:
            row = s.exec(select(LLMCallLog)).one()
            assert row.source == "translate"
            assert row.install_id == "logged-install-0001-aaaa"
            assert row.output_tokens == 380
            assert row.cost_usd is not None

    def test_cache_hit_records_nothing(self, monkeypatch) -> None:
        """A cache hit costs nothing, so it must not appear in the cost log."""
        monkeypatch.setattr(
            translate_router,
            "translate_problem",
            lambda _t, settings=None: TranslationResult(
                problem=SimplifiedProblem.model_validate(VALID),
                provider=ProviderName.GROQ,
                model="openai/gpt-oss-120b",
                input_tokens=1,
                output_tokens=1,
            ),
        )
        body = {
            "install_id": "cache-log-install-0001-aa",
            "raw_text": "Valid Parentheses\n\nGiven a string of brackets, say if it is valid.",
        }
        client.post("/translate", json=body)   # miss -> logged
        client.post("/translate", json=body)   # hit  -> not logged
        client.post("/translate", json=body)   # hit  -> not logged

        from app.db import get_engine

        with Session(get_engine()) as s:
            assert len(s.exec(select(LLMCallLog)).all()) == 1


class TestSentryScrubbing:
    def test_pasted_problem_text_is_removed(self) -> None:
        event = {
            "request": {
                "data": {
                    "install_id": "abc-123",
                    "raw_text": "the user's entire pasted problem statement",
                },
                "cookies": {"session": "secret"},
            }
        }
        scrubbed = _scrub_event(event, {})

        assert scrubbed["request"]["data"]["raw_text"] == "[scrubbed]"
        # The install id is anonymous and useful for debugging, so it stays.
        assert scrubbed["request"]["data"]["install_id"] == "abc-123"
        assert "cookies" not in scrubbed["request"]

    def test_nested_and_extra_fields_are_scrubbed(self) -> None:
        event = {"extra": {"payload": {"raw_text": "pasted text", "n": 1}}}
        scrubbed = _scrub_event(event, {})
        assert scrubbed["extra"]["payload"]["raw_text"] == "[scrubbed]"
        assert scrubbed["extra"]["payload"]["n"] == 1

    def test_event_without_request_is_untouched(self) -> None:
        assert _scrub_event({"message": "hello"}, {}) == {"message": "hello"}

    def test_init_is_a_no_op_without_a_dsn(self) -> None:
        assert init_sentry(Settings(sentry_dsn="")) is False

    def test_pii_is_off_by_default(self) -> None:
        """Sending IPs and headers to a third party is a disclosure decision."""
        assert Settings().sentry_send_pii is False


class TestJsonLogging:
    def test_fields_are_merged_into_the_json_object(self, caplog) -> None:
        logger = logging.getLogger("test.json")
        formatter = JsonLogFormatter()

        with caplog.at_level(logging.INFO, logger="test.json"):
            log_event(logger, "llm_call", provider="groq", cost_usd=0.0004)

        payload = json.loads(formatter.format(caplog.records[0]))
        assert payload["message"] == "llm_call"
        assert payload["provider"] == "groq"
        assert payload["cost_usd"] == 0.0004
        assert payload["level"] == "INFO"
