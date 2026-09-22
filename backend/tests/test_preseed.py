"""Tests for the pre-seed script's file loader.

The seeding loop itself is exercised against the real script in manual runs;
what is worth pinning here is the input parsing, because the seed file is
hand-maintained and a malformed row should fail loudly with a usable message
rather than silently seeding nothing.
"""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "preseed.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("preseed", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preseed = _load_module()


class TestLoadEntries:
    def test_json_title_and_body(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.json"
        path.write_text(
            json.dumps([{"title": "Two Sum", "body": "Given an array..."}]),
            encoding="utf-8",
        )
        entries = preseed.load_entries(path)

        assert len(entries) == 1
        assert entries[0].title == "Two Sum"
        # Title on the first line is what extract_title() and the daily job use.
        assert entries[0].raw_text == "Two Sum\n\nGiven an array..."

    def test_json_raw_text_only(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.json"
        path.write_text(
            json.dumps([{"raw_text": "Valid Parentheses\n\nGiven a string s..."}]),
            encoding="utf-8",
        )
        entries = preseed.load_entries(path)
        assert entries[0].title == "Valid Parentheses"

    def test_csv_title_and_body(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.csv"
        path.write_text(
            "title,body\nTwo Sum,Given an array of integers\n"
            "Binary Search,Given a sorted array\n",
            encoding="utf-8",
        )
        entries = preseed.load_entries(path)

        assert [e.title for e in entries] == ["Two Sum", "Binary Search"]
        assert entries[1].raw_text.startswith("Binary Search\n\n")

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(preseed.SeedFileError, match="not found"):
            preseed.load_entries(tmp_path / "nope.json")

    def test_malformed_json(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(preseed.SeedFileError, match="not valid JSON"):
            preseed.load_entries(path)

    def test_json_must_be_a_list(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.json"
        path.write_text(json.dumps({"title": "Two Sum"}), encoding="utf-8")
        with pytest.raises(preseed.SeedFileError, match="must contain a JSON list"):
            preseed.load_entries(path)

    def test_entry_missing_fields_names_the_index(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.json"
        path.write_text(
            json.dumps([{"title": "Two Sum", "body": "ok"}, {"title": "No Body"}]),
            encoding="utf-8",
        )
        with pytest.raises(preseed.SeedFileError, match="entry 1"):
            preseed.load_entries(path)

    def test_unsupported_extension(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.yaml"
        path.write_text("- title: Two Sum", encoding="utf-8")
        with pytest.raises(preseed.SeedFileError, match="unsupported seed file type"):
            preseed.load_entries(path)

    def test_duplicates_are_warned_not_fatal(self, tmp_path: Path, caplog) -> None:
        path = tmp_path / "seed.json"
        path.write_text(
            json.dumps(
                [
                    {"title": "Two Sum", "body": "Given an array..."},
                    {"title": "Two Sum", "body": "Given an array..."},
                ]
            ),
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            entries = preseed.load_entries(path)

        assert len(entries) == 2
        assert "duplicate problem text" in caplog.text


def test_shipped_seed_file_is_valid() -> None:
    """The file we ship must parse and produce title-first raw text."""
    entries = preseed.load_entries(preseed.DEFAULT_SEED_FILE)

    assert len(entries) >= 10
    for entry in entries:
        assert entry.title
        assert entry.raw_text.splitlines()[0] == entry.title
        # Long enough to clear TranslateRequest's 20-char floor.
        assert len(entry.raw_text) > 100
