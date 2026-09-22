"""Tests for hash derivation, title extraction and cache lookup."""

import pytest

from app.cache import (
    compute_hash,
    extract_title,
    find_cached,
    normalize_text,
    store_translation,
)
from app.schemas import SimplifiedProblem
from tests.test_schemas import VALID

TWO_SUM = (
    "Two Sum\n\n"
    "Given an array of integers nums and an integer target, return indices of "
    "the two numbers such that they add up to target.\n\n"
    "Example 1:\nInput: nums = [2,7,11,15], target = 9\nOutput: [0,1]"
)


@pytest.fixture
def problem() -> SimplifiedProblem:
    return SimplifiedProblem.model_validate(VALID)


class TestNormalizationAndHashing:
    def test_hash_is_stable(self) -> None:
        assert compute_hash(TWO_SUM) == compute_hash(TWO_SUM)
        assert len(compute_hash(TWO_SUM)) == 64

    @pytest.mark.parametrize(
        "variant",
        [
            "   " + TWO_SUM + "   ",          # padded
            TWO_SUM.upper(),                   # different case
            TWO_SUM.replace("\n\n", "\n\n\n"),  # extra blank line
            TWO_SUM.replace(" ", "  "),        # doubled spaces
        ],
    )
    def test_incidental_differences_hash_the_same(self, variant: str) -> None:
        assert compute_hash(variant) == compute_hash(TWO_SUM)

    def test_different_problems_hash_differently(self) -> None:
        assert compute_hash(TWO_SUM) != compute_hash("Valid Parentheses\n\nGiven a string...")

    def test_punctuation_is_preserved(self) -> None:
        """`[]` vs `()` can change the problem, so normalization leaves them alone."""
        assert normalize_text("chars []") != normalize_text("chars ()")


class TestTitleExtraction:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Two Sum\n\nGiven an array...", "Two Sum"),
            ("\n\n  Two Sum  \nGiven an array...", "Two Sum"),
            ("1. Two Sum\nGiven an array...", "Two Sum"),
            ("#1 Two Sum\nGiven an array...", "Two Sum"),
            ("42) Trapping Rain Water\nGiven...", "Trapping Rain Water"),
            ("Two Sum - Easy\nGiven an array...", "Two Sum"),
            ("Two Sum Medium\nGiven an array...", "Two Sum"),
            ("", ""),
            ("   \n  \n", ""),
        ],
    )
    def test_extract_title(self, raw: str, expected: str) -> None:
        assert extract_title(raw) == expected


class TestCacheLookup:
    def test_miss_on_empty_cache(self, session) -> None:
        assert find_cached(session, TWO_SUM) is None

    def test_store_then_hit_by_hash(self, session, problem) -> None:
        store_translation(session, raw_text=TWO_SUM, problem=problem)

        hit = find_cached(session, TWO_SUM)
        assert hit is not None
        assert hit.problem_title == "Two Sum"
        assert hit.is_preseeded is False
        assert SimplifiedProblem.model_validate(hit.simplified_json) == problem

    def test_hit_survives_whitespace_and_case_differences(self, session, problem) -> None:
        store_translation(session, raw_text=TWO_SUM, problem=problem)
        assert find_cached(session, "  " + TWO_SUM.upper() + "  ") is not None

    def test_title_fallback_hits_preseeded_rows(self, session, problem) -> None:
        """A different paste of a curated problem should still hit."""
        store_translation(
            session, raw_text=TWO_SUM, problem=problem, is_preseeded=True
        )
        # Same problem, materially different body: the hash cannot match.
        other_paste = "Two Sum\n\nCompletely different wording of the body text here."
        assert compute_hash(other_paste) != compute_hash(TWO_SUM)

        hit = find_cached(session, other_paste)
        assert hit is not None and hit.is_preseeded is True

    def test_title_fallback_ignores_non_preseeded_rows(self, session, problem) -> None:
        """Organically cached titles are not trusted for cross-user matching."""
        store_translation(
            session, raw_text=TWO_SUM, problem=problem, is_preseeded=False
        )
        other_paste = "Two Sum\n\nCompletely different wording of the body text here."
        assert find_cached(session, other_paste) is None

    def test_storing_twice_does_not_duplicate(self, session, problem) -> None:
        first = store_translation(session, raw_text=TWO_SUM, problem=problem)
        second = store_translation(session, raw_text=TWO_SUM, problem=problem)
        assert first.id == second.id

    def test_hit_updates_last_used_at(self, session, problem) -> None:
        row = store_translation(session, raw_text=TWO_SUM, problem=problem)
        original = row.last_used_at

        refreshed = find_cached(session, TWO_SUM)
        assert refreshed is not None
        assert refreshed.last_used_at >= original
