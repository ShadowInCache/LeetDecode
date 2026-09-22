"""Tests for the LLM output contract.

The point of these is the SRS rule that a schema-mismatched LLM response is
*rejected*, never coerced into something that looks valid. Each rejection case
below is a real shape a model has been known to return.
"""

import json

import pytest
from pydantic import ValidationError

from app.schemas import (
    SimplifiedProblem,
    TranslateRequest,
    TranslateResponse,
    gemini_json_schema,
)

VALID: dict = {
    "what_you_need_to_do": "Find two numbers in the list that add up to the target.",
    "input": "A list of whole numbers and a target number.",
    "output": "The positions of the two numbers that add up to the target.",
    "important_notes": [
        "Exactly one valid answer is guaranteed to exist.",
        "You cannot use the same item twice.",
    ],
    "example": {
        "input": "numbers = [2, 7, 11, 15], target = 9",
        "output": "[0, 1]",
        "explanation": "2 + 7 = 9, and those sit at positions 0 and 1.",
    },
}


def test_valid_payload_round_trips() -> None:
    problem = SimplifiedProblem.model_validate(VALID)
    assert problem.example.output == "[0, 1]"
    assert len(problem.important_notes) == 2
    # Must survive a JSON round trip, since this is what lands in JSONB.
    assert SimplifiedProblem.model_validate_json(problem.model_dump_json()) == problem


def test_surrounding_whitespace_is_trimmed() -> None:
    problem = SimplifiedProblem.model_validate({**VALID, "input": "  padded  "})
    assert problem.input == "padded"


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("extra top-level key", {**VALID, "difficulty": "easy"}),
        ("extra nested key", {**VALID, "example": {**VALID["example"], "big_o": "n"}}),
        ("missing key", {k: v for k, v in VALID.items() if k != "input"}),
        ("int where string expected", {**VALID, "input": 12345}),
        ("string where list expected", {**VALID, "important_notes": "only one note"}),
        ("empty notes list", {**VALID, "important_notes": []}),
        ("whitespace-only string", {**VALID, "output": "   "}),
        ("empty string", {**VALID, "what_you_need_to_do": ""}),
        ("null inside notes", {**VALID, "important_notes": [None]}),
        ("example sent as string", {**VALID, "example": "see above"}),
        ("example missing explanation", {**VALID, "example": {"input": "a", "output": "b"}}),
    ],
)
def test_malformed_payloads_are_rejected(label: str, payload: dict) -> None:
    with pytest.raises(ValidationError):
        SimplifiedProblem.model_validate(payload)


class TestGeminiSchema:
    """The schema we hand to Gemini must be portable and self-contained."""

    def test_no_refs_or_unsupported_keywords(self) -> None:
        blob = json.dumps(gemini_json_schema())
        for keyword in ("$ref", "$defs", "minLength", "minItems", "additionalProperties"):
            assert keyword not in blob, f"{keyword} leaked into the provider schema"

    def test_nested_example_is_inlined(self) -> None:
        example = gemini_json_schema()["properties"]["example"]
        assert example["type"] == "object"
        assert sorted(example["properties"]) == ["explanation", "input", "output"]

    def test_required_fields_and_descriptions_survive(self) -> None:
        schema = gemini_json_schema()
        assert sorted(schema["required"]) == [
            "example",
            "important_notes",
            "input",
            "output",
            "what_you_need_to_do",
        ]
        assert schema["properties"]["what_you_need_to_do"]["description"]

    def test_internal_docstring_not_leaked_to_provider(self) -> None:
        assert "description" not in gemini_json_schema()


class TestTranslateRequest:
    def test_accepts_a_realistic_request(self) -> None:
        req = TranslateRequest(
            install_id="3f2b1c9a-7d4e-4f1a-9c2b-8e5d6a7b3c1d",
            raw_text="Two Sum\n\nGiven an array of integers nums and an integer target...",
        )
        assert req.install_id.startswith("3f2b")

    @pytest.mark.parametrize(
        ("label", "kwargs"),
        [
            ("short install_id", {"install_id": "abc", "raw_text": "x" * 50}),
            ("blank raw_text", {"install_id": "a" * 36, "raw_text": "   "}),
            ("raw_text too short", {"install_id": "a" * 36, "raw_text": "Two Sum"}),
            ("raw_text over cap", {"install_id": "a" * 36, "raw_text": "x" * 20_001}),
        ],
    )
    def test_rejects_bad_requests(self, label: str, kwargs: dict) -> None:
        with pytest.raises(ValidationError):
            TranslateRequest(**kwargs)


def test_translate_response_source_is_constrained() -> None:
    ok = TranslateResponse(source="cache", data=SimplifiedProblem.model_validate(VALID))
    assert ok.source == "cache"
    with pytest.raises(ValidationError):
        TranslateResponse(source="guess", data=SimplifiedProblem.model_validate(VALID))


class TestGroqSchema:
    """Groq's strict mode needs the opposite of what Gemini needs."""

    def test_keeps_additional_properties_false(self) -> None:
        from app.schemas import groq_json_schema

        schema = groq_json_schema()
        # `strict: true` on Groq requires this; Gemini rejects it.
        assert schema["additionalProperties"] is False
        assert schema["properties"]["example"]["additionalProperties"] is False

    def test_still_drops_refs_and_constraints(self) -> None:
        import json as _json

        from app.schemas import groq_json_schema

        blob = _json.dumps(groq_json_schema())
        for keyword in ("$ref", "$defs", "minLength", "minItems", "title"):
            assert keyword not in blob

    def test_the_two_providers_get_different_schemas(self) -> None:
        from app.schemas import gemini_json_schema, groq_json_schema

        assert "additionalProperties" not in gemini_json_schema()
        assert "additionalProperties" in groq_json_schema()
