"""Tests for the configuration preflight checks.

These encode the deploy mistakes that are expensive because they are *quiet* -
the service starts, health checks pass, and only real traffic reveals the
problem.
"""

import pytest

from app.config import ProviderName, Settings
from app.preflight import Level, run


def levels(report) -> dict[str, Level]:
    return {c.name: c.level for c in report.checks}


class TestProviderChecks:
    def test_no_keys_at_all_is_a_failure(self) -> None:
        """The shared-variables trap: defined in Railway, never shared to the service."""
        report = run(
            Settings(gemini_api_key="", groq_api_key="", database_url=""),
            include_database=False,
        )
        assert levels(report)["llm providers"] is Level.FAIL
        detail = next(c.detail for c in report.checks if c.name == "llm providers")
        # The message must point at the actual cause, not just say "missing".
        assert "shared into the service" in detail

    def test_both_keys_present_is_ok(self) -> None:
        report = run(
            Settings(gemini_api_key="x", groq_api_key="y"), include_database=False
        )
        assert levels(report)["llm providers"] is Level.OK

    def test_missing_fallback_key_is_only_a_warning(self) -> None:
        """Losing failover degrades the service; it doesn't break it."""
        report = run(
            Settings(gemini_api_key="x", groq_api_key=""), include_database=False
        )
        assert levels(report)["llm providers"] is Level.WARN
        assert report.ok

    def test_missing_primary_but_present_fallback_still_works(self) -> None:
        report = run(
            Settings(gemini_api_key="", groq_api_key="y"), include_database=False
        )
        assert levels(report)["llm providers"] is Level.WARN
        assert report.ok

    def test_unpriced_model_is_flagged(self) -> None:
        report = run(
            Settings(gemini_api_key="x", gemini_model="some-unpriced-model"),
            include_database=False,
        )
        assert levels(report)["pricing/gemini"] is Level.WARN


class TestDatabaseChecks:
    def test_missing_url_is_a_failure(self) -> None:
        report = run(Settings(gemini_api_key="x", database_url=""))
        assert levels(report)["database"] is Level.FAIL

    def test_unreachable_database_is_a_failure(self) -> None:
        from app import db

        db.set_engine(None)
        report = run(
            Settings(
                gemini_api_key="x",
                database_url="postgresql://nobody:nobody@127.0.0.1:1/nothing",
            )
        )
        db.set_engine(None)
        assert levels(report)["database"] is Level.FAIL

    def test_reachable_database_reports_its_tables(self, engine) -> None:
        report = run(Settings(gemini_api_key="x", database_url="sqlite://"))
        assert levels(report)["database"] is Level.OK
        assert levels(report)["schema"] is Level.OK


class TestOperationalChecks:
    def test_rate_limits_off_in_production_is_a_failure(self) -> None:
        """An open, unmetered door to a paid API is not a warning."""
        report = run(
            Settings(
                gemini_api_key="x",
                rate_limits_enabled=False,
                sentry_environment="production",
            ),
            include_database=False,
        )
        assert levels(report)["rate limits"] is Level.FAIL
        assert not report.ok

    def test_rate_limits_off_in_development_is_fine(self) -> None:
        report = run(
            Settings(
                gemini_api_key="x",
                rate_limits_enabled=False,
                sentry_environment="development",
            ),
            include_database=False,
        )
        assert levels(report)["rate limits"] is Level.WARN
        assert report.ok

    def test_missing_admin_token_warns_that_the_dashboard_is_off(self) -> None:
        report = run(
            Settings(gemini_api_key="x", admin_token=""), include_database=False
        )
        assert levels(report)["admin dashboard"] is Level.WARN

    def test_pii_in_production_is_flagged(self) -> None:
        report = run(
            Settings(
                gemini_api_key="x",
                sentry_environment="production",
                sentry_send_pii=True,
            ),
            include_database=False,
        )
        assert levels(report)["privacy"] is Level.WARN

    def test_a_correct_production_config_passes_cleanly(self, engine) -> None:
        report = run(
            Settings(
                gemini_api_key="x",
                groq_api_key="y",
                database_url="sqlite://",
                admin_token="t" * 32,
                sentry_dsn="https://example@sentry.io/1",
                sentry_environment="production",
            )
        )
        assert report.ok
        assert not report.warnings, [c.detail for c in report.warnings]


def test_report_ok_is_false_only_on_failures() -> None:
    report = run(Settings(gemini_api_key="x", groq_api_key=""), include_database=False)
    assert report.warnings
    assert report.ok


@pytest.mark.parametrize("provider", list(ProviderName))
def test_default_models_are_all_priced(provider: ProviderName) -> None:
    """A shipped default without a price would make the dashboard blind."""
    from app.pricing import PRICES

    assert Settings().model_for(provider) in PRICES


class TestPastedSecretHygiene:
    """A trailing newline on a pasted secret must not reach the driver.

    This is not hypothetical: it cost a real deploy cycle, surfacing as
    `FATAL: database "postgres\n" does not exist` - which reads like a missing
    database rather than a stray character.
    """

    def test_trailing_newline_is_stripped_from_database_url(self) -> None:
        from urllib.parse import urlparse

        settings = Settings(
            database_url="postgresql://u:p@host.pooler.supabase.com:5432/postgres\n"
        )
        assert urlparse(settings.database_url).path.lstrip("/") == "postgres"

    def test_whitespace_is_stripped_from_api_keys(self) -> None:
        settings = Settings(
            gemini_api_key="  gem-key\n", groq_api_key="\tgsk_key  ",
        )
        assert settings.gemini_api_key == "gem-key"
        assert settings.groq_api_key == "gsk_key"

    def test_admin_token_and_dsn_are_stripped(self) -> None:
        settings = Settings(admin_token=" tok\n", sentry_dsn="https://x@y/1\n")
        assert settings.admin_token == "tok"
        assert settings.sentry_dsn == "https://x@y/1"

    def test_a_whitespace_only_key_still_counts_as_unset(self) -> None:
        """Otherwise preflight would report a provider as configured."""
        settings = Settings(gemini_api_key="   \n", groq_api_key="")
        report = run(settings, include_database=False)
        assert levels(report)["llm providers"] is Level.FAIL


class TestProviderNameTolerance:
    """A mistyped provider name must not take the service down.

    `LLM_PROVIDER=Groq` crashed production: `Settings()` is built while
    `app.main` imports, so a ValidationError there means uvicorn never starts
    and the platform serves 502s with no application log to explain them.
    """

    @pytest.mark.parametrize(
        "value", ["groq", "Groq", "GROQ", " groq ", "groq\n", "\tGroq"]
    )
    def test_casing_and_whitespace_are_tolerated(self, value: str) -> None:
        assert Settings(llm_provider=value).llm_provider is ProviderName.GROQ

    @pytest.mark.parametrize("value", ["gemini", "Gemini", "GEMINI", " gemini"])
    def test_the_other_provider_too(self, value: str) -> None:
        assert Settings(llm_provider=value).llm_provider is ProviderName.GEMINI

    def test_empty_primary_falls_back_to_the_default(self) -> None:
        assert Settings(llm_provider="").llm_provider is ProviderName.GEMINI

    def test_empty_fallback_means_failover_disabled(self) -> None:
        assert Settings(llm_fallback_provider="").llm_fallback_provider is None

    def test_fallback_accepts_odd_casing_too(self) -> None:
        settings = Settings(llm_fallback_provider="GEMINI")
        assert settings.llm_fallback_provider is ProviderName.GEMINI

    def test_an_unknown_provider_is_still_an_error(self) -> None:
        """Tolerant of typos in form, not of genuinely wrong values."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="not a known provider"):
            Settings(llm_provider="openai")

    def test_the_error_names_the_valid_options(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            Settings(llm_provider="anthropic")
        message = str(exc.value)
        assert "gemini" in message and "groq" in message
