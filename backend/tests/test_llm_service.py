"""Tests for the validation gate and provider failover.

These use fake providers, so they run without any API key. The point is to pin
down the two behaviours that protect the cache:

  * schema-invalid output is a failure, never a partial result;
  * a failing primary provider falls through to the fallback exactly once.
"""

import json

import pytest

from app.config import ProviderName, Settings
from app.llm import service
from app.llm.base import (
    AllProvidersFailed,
    InvalidLLMOutput,
    ProviderCallFailed,
    ProviderNotConfigured,
)
from app.llm.service import translate_problem, validate_output
from tests.test_schemas import VALID

VALID_JSON = json.dumps(VALID)


class FakeProvider:
    """Returns canned text, or raises, and records that it was called."""

    def __init__(self, name: ProviderName, *, returns: str = "", raises: Exception | None = None):
        self.name = name
        self._returns = returns
        self._raises = raises
        self.calls = 0

    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        if self._raises is not None:
            raise self._raises
        return self._returns


@pytest.fixture
def settings() -> Settings:
    """Gemini primary, Groq fallback, both with dummy credentials."""
    return Settings(
        gemini_api_key="dummy-gemini",
        groq_api_key="dummy-groq",
        llm_provider=ProviderName.GEMINI,
        llm_fallback_provider=ProviderName.GROQ,
    )


@pytest.fixture
def install_providers(monkeypatch):
    """Swap the real adapters for fakes."""

    def _install(**by_name: FakeProvider):
        monkeypatch.setattr(
            service,
            "PROVIDER_FACTORIES",
            {name: (lambda _s, p=prov: p) for name, prov in by_name.items()},
        )
        return by_name

    return _install


# ---------------------------------------------------------------------------
# The validation gate
# ---------------------------------------------------------------------------


class TestValidateOutput:
    def test_accepts_plain_json(self) -> None:
        assert validate_output(VALID_JSON).example.output == "[0, 1]"

    @pytest.mark.parametrize(
        "wrapped",
        [
            "```json\n{payload}\n```",
            "```\n{payload}\n```",
            "  {payload}  ",
        ],
    )
    def test_tolerates_transport_wrappers(self, wrapped: str) -> None:
        """Fences are stripped - but only the wrapper, never the contract."""
        assert validate_output(wrapped.format(payload=VALID_JSON)).input

    def test_rejects_unparseable_json(self) -> None:
        with pytest.raises(InvalidLLMOutput, match="not valid JSON"):
            validate_output("Sure! Here's the simplified problem:")

    def test_rejects_schema_violation_and_names_the_field(self) -> None:
        broken = json.loads(VALID_JSON)
        del broken["important_notes"]
        with pytest.raises(InvalidLLMOutput, match="important_notes"):
            validate_output(json.dumps(broken))

    def test_rejects_extra_keys(self) -> None:
        extra = {**json.loads(VALID_JSON), "optimal_solution": "use a hash map"}
        with pytest.raises(InvalidLLMOutput, match="did not match the schema"):
            validate_output(json.dumps(extra))


# ---------------------------------------------------------------------------
# Failover
# ---------------------------------------------------------------------------


class TestFailover:
    def test_primary_success_does_not_touch_fallback(self, settings, install_providers):
        p = install_providers(
            **{
                ProviderName.GEMINI: FakeProvider(ProviderName.GEMINI, returns=VALID_JSON),
                ProviderName.GROQ: FakeProvider(ProviderName.GROQ, returns=VALID_JSON),
            }
        )
        result = translate_problem("Two Sum\nGiven an array...", settings=settings)

        assert result.provider is ProviderName.GEMINI
        assert p[ProviderName.GEMINI].calls == 1
        assert p[ProviderName.GROQ].calls == 0

    def test_transport_failure_falls_through_to_groq(self, settings, install_providers):
        p = install_providers(
            **{
                ProviderName.GEMINI: FakeProvider(
                    ProviderName.GEMINI, raises=ProviderCallFailed("429 rate limited")
                ),
                ProviderName.GROQ: FakeProvider(ProviderName.GROQ, returns=VALID_JSON),
            }
        )
        result = translate_problem("Two Sum\nGiven an array...", settings=settings)

        assert result.provider is ProviderName.GROQ
        assert p[ProviderName.GEMINI].calls == 1
        assert p[ProviderName.GROQ].calls == 1

    def test_schema_invalid_primary_also_falls_through(self, settings, install_providers):
        """A model that ignores the contract is treated like one that is down."""
        p = install_providers(
            **{
                ProviderName.GEMINI: FakeProvider(
                    ProviderName.GEMINI, returns='{"what_you_need_to_do": "only this"}'
                ),
                ProviderName.GROQ: FakeProvider(ProviderName.GROQ, returns=VALID_JSON),
            }
        )
        result = translate_problem("Two Sum\nGiven an array...", settings=settings)

        assert result.provider is ProviderName.GROQ
        assert p[ProviderName.GROQ].calls == 1

    def test_both_failing_raises_with_both_causes(self, settings, install_providers):
        install_providers(
            **{
                ProviderName.GEMINI: FakeProvider(
                    ProviderName.GEMINI, raises=ProviderCallFailed("boom")
                ),
                ProviderName.GROQ: FakeProvider(ProviderName.GROQ, returns="not json"),
            }
        )
        with pytest.raises(AllProvidersFailed) as exc_info:
            translate_problem("Two Sum\nGiven an array...", settings=settings)

        failures = exc_info.value.failures
        assert set(failures) == {ProviderName.GEMINI, ProviderName.GROQ}
        assert isinstance(failures[ProviderName.GEMINI], ProviderCallFailed)
        assert isinstance(failures[ProviderName.GROQ], InvalidLLMOutput)

    def test_fallback_disabled_means_one_attempt(self, install_providers):
        settings = Settings(
            gemini_api_key="dummy-gemini",
            llm_provider=ProviderName.GEMINI,
            llm_fallback_provider=None,
        )
        p = install_providers(
            **{
                ProviderName.GEMINI: FakeProvider(
                    ProviderName.GEMINI, raises=ProviderCallFailed("boom")
                ),
                ProviderName.GROQ: FakeProvider(ProviderName.GROQ, returns=VALID_JSON),
            }
        )
        with pytest.raises(AllProvidersFailed):
            translate_problem("Two Sum\nGiven an array...", settings=settings)
        assert p[ProviderName.GROQ].calls == 0

    def test_unconfigured_primary_is_skipped_not_fatal(self, install_providers, monkeypatch):
        """A missing GEMINI_API_KEY should degrade to Groq, not 502."""
        settings = Settings(
            gemini_api_key="",
            groq_api_key="dummy-groq",
            llm_provider=ProviderName.GEMINI,
            llm_fallback_provider=ProviderName.GROQ,
        )

        def gemini_factory(_s):
            raise ProviderNotConfigured("GEMINI_API_KEY is not set")

        groq = FakeProvider(ProviderName.GROQ, returns=VALID_JSON)
        monkeypatch.setattr(
            service,
            "PROVIDER_FACTORIES",
            {ProviderName.GEMINI: gemini_factory, ProviderName.GROQ: lambda _s: groq},
        )

        result = translate_problem("Two Sum\nGiven an array...", settings=settings)
        assert result.provider is ProviderName.GROQ

    def test_problem_text_reaches_the_provider(self, settings, install_providers):
        p = install_providers(
            **{ProviderName.GEMINI: FakeProvider(ProviderName.GEMINI, returns=VALID_JSON)}
        )
        translate_problem("Valid Parentheses\nGiven a string s...", settings=settings)

        prompt = p[ProviderName.GEMINI].user_prompt
        assert "Valid Parentheses" in prompt
        assert "<problem>" in prompt and "</problem>" in prompt
