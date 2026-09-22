"""Tests for the daily-problem job.

The network call is mocked; these cover response parsing, HTML flattening, and
the job's two safety properties: it never calls the LLM for an already-cached
problem, and it never raises (it runs unattended on a timer).
"""

import json

import httpx
import pytest

from app.config import ProviderName, Settings
from app.daily import (
    DailyProblem,
    DailyProblemError,
    fetch_daily_problem,
    html_to_text,
    refresh_daily_problem,
)
from app.llm.base import AllProvidersFailed, ProviderCallFailed
from app.llm.service import TranslationResult
from app.schemas import SimplifiedProblem
from tests.test_schemas import VALID

QUESTION_HTML = (
    "<p>Given an array of integers <code>nums</code>, return indices.</p>"
    "<p>&nbsp;</p>"
    "<p><strong>Example 1:</strong></p>"
    "<pre><strong>Input:</strong> nums = [2,7,11,15]\n<strong>Output:</strong> [0,1]</pre>"
    "<ul><li>Only one valid answer exists.</li></ul>"
)


def graphql_body(content: str | None = QUESTION_HTML) -> dict:
    return {
        "data": {
            "activeDailyCodingChallengeQuestion": {
                "date": "2026-09-22",
                "link": "/problems/two-sum/",
                "question": {
                    "title": "Two Sum",
                    "titleSlug": "two-sum",
                    "difficulty": "Easy",
                    "content": content,
                },
            }
        }
    }


@pytest.fixture
def mock_leetcode(monkeypatch):
    """Patch httpx.post with a canned GraphQL response."""

    def _install(body: dict | None = None, *, status: int = 200, raise_exc=None):
        def fake_post(url, **kwargs):
            if raise_exc is not None:
                raise raise_exc
            request = httpx.Request("POST", url)
            return httpx.Response(
                status_code=status,
                content=json.dumps(body if body is not None else graphql_body()),
                headers={"Content-Type": "application/json"},
                request=request,
            )

        monkeypatch.setattr(httpx, "post", fake_post)

    return _install


class TestHtmlToText:
    def test_tags_are_stripped_and_structure_kept(self) -> None:
        text = html_to_text(QUESTION_HTML)
        assert "<p>" not in text and "<strong>" not in text
        assert "Given an array of integers nums, return indices." in text
        assert "Only one valid answer exists." in text

    def test_entities_are_decoded(self) -> None:
        assert html_to_text("<p>a &lt; b &amp;&amp; c &gt; d</p>") == "a < b && c > d"

    def test_blank_line_runs_are_collapsed(self) -> None:
        assert "\n\n\n" not in html_to_text("<div><p></p><p></p>text</div>")


class TestFetchDailyProblem:
    def test_parses_a_well_formed_response(self, mock_leetcode) -> None:
        mock_leetcode()
        daily = fetch_daily_problem()

        assert daily.title == "Two Sum"
        assert daily.slug == "two-sum"
        assert daily.date == "2026-09-22"
        assert "<p>" not in daily.content

    def test_raw_text_looks_like_a_user_paste(self, mock_leetcode) -> None:
        """First line must be the title, so the title fallback can match it."""
        mock_leetcode()
        raw = fetch_daily_problem().as_raw_text()
        assert raw.splitlines()[0] == "Two Sum"

    def test_premium_problem_with_null_content_is_an_error(self, mock_leetcode) -> None:
        mock_leetcode(graphql_body(content=None))
        with pytest.raises(DailyProblemError, match="no public content"):
            fetch_daily_problem()

    def test_graphql_errors_are_surfaced(self, mock_leetcode) -> None:
        mock_leetcode({"errors": [{"message": "rate limited"}]})
        with pytest.raises(DailyProblemError, match="GraphQL errors"):
            fetch_daily_problem()

    def test_unexpected_shape_is_an_error(self, mock_leetcode) -> None:
        mock_leetcode({"data": {"activeDailyCodingChallengeQuestion": None}})
        with pytest.raises(DailyProblemError, match="unexpected GraphQL response"):
            fetch_daily_problem()

    def test_network_failure_is_an_error(self, mock_leetcode) -> None:
        mock_leetcode(raise_exc=httpx.ConnectError("no route to host"))
        with pytest.raises(DailyProblemError, match="request failed"):
            fetch_daily_problem()

    def test_http_error_status_is_an_error(self, mock_leetcode) -> None:
        mock_leetcode(status=503)
        with pytest.raises(DailyProblemError):
            fetch_daily_problem()


class TestRefreshDailyProblem:
    @pytest.fixture
    def settings(self, engine) -> Settings:
        # `engine` makes app.db hand out the in-memory test database.
        return Settings(gemini_api_key="dummy", database_url="sqlite://")

    def test_caches_the_problem_as_preseeded(self, mock_leetcode, settings, monkeypatch):
        mock_leetcode()
        calls = {"n": 0}

        def fake_translate(_text, settings=None):
            calls["n"] += 1
            return TranslationResult(
                problem=SimplifiedProblem.model_validate(VALID),
                provider=ProviderName.GEMINI,
            )

        monkeypatch.setattr("app.daily.translate_problem", fake_translate)
        assert refresh_daily_problem(settings) is True
        assert calls["n"] == 1

        # Preseeded, so it is free for everyone forever.
        from sqlmodel import Session, select

        from app.db import get_engine
        from app.models import ProblemCache

        with Session(get_engine(settings)) as s:
            row = s.exec(select(ProblemCache)).one()
            assert row.is_preseeded is True
            assert row.problem_title == "Two Sum"

    def test_rerun_does_not_call_the_llm_again(self, mock_leetcode, settings, monkeypatch):
        """The job is safe to run repeatedly - that is what makes multi-instance ok."""
        mock_leetcode()
        calls = {"n": 0}

        def fake_translate(_text, settings=None):
            calls["n"] += 1
            return TranslationResult(
                problem=SimplifiedProblem.model_validate(VALID),
                provider=ProviderName.GEMINI,
            )

        monkeypatch.setattr("app.daily.translate_problem", fake_translate)

        assert refresh_daily_problem(settings) is True
        assert refresh_daily_problem(settings) is True
        assert calls["n"] == 1

    def test_returns_false_instead_of_raising_on_fetch_failure(
        self, mock_leetcode, settings
    ):
        mock_leetcode(raise_exc=httpx.ConnectError("down"))
        assert refresh_daily_problem(settings) is False

    def test_returns_false_instead_of_raising_on_llm_failure(
        self, mock_leetcode, settings, monkeypatch
    ):
        mock_leetcode()

        def blow_up(_text, settings=None):
            raise AllProvidersFailed({ProviderName.GEMINI: ProviderCallFailed("down")})

        monkeypatch.setattr("app.daily.translate_problem", blow_up)
        assert refresh_daily_problem(settings) is False


def test_daily_problem_raw_text_shape() -> None:
    daily = DailyProblem(
        date="2026-09-22", title="Two Sum", slug="two-sum",
        difficulty="Easy", content="Body text here.",
    )
    assert daily.as_raw_text() == "Two Sum\n\nBody text here."
