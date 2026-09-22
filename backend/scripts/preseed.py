"""Batch-translate a list of common problems into `problems_cache`.

Standalone: not imported by the running API. It uses the same
`translate_problem()` and the same `SimplifiedProblem` gate as `/translate`, so
preseeded rows are indistinguishable in quality from organically cached ones.

Usage:
    python scripts/preseed.py                          # data/seed_problems.json
    python scripts/preseed.py --file my_problems.csv
    python scripts/preseed.py --dry-run                # parse + report, no LLM
    python scripts/preseed.py --limit 5                # try a handful first
    python scripts/preseed.py --delay 1.0              # pace the provider

Safe to re-run: anything already cached (matched by hash) is skipped without
calling the LLM, so an interrupted run resumes where it stopped.

Input formats
-------------
JSON: a list of objects with "title" and "body", or with a single "raw_text".
CSV:  a header row containing `title,body` (or a single `raw_text` column).

When title and body are given separately they are joined as "title\\n\\nbody",
which puts the title on the first line - the shape `extract_title()` reads and
the same shape the daily job produces.
"""

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session  # noqa: E402

from app.cache import compute_hash, find_cached, store_translation  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import create_db_and_tables, get_engine  # noqa: E402
from app.llm import AllProvidersFailed, translate_problem  # noqa: E402

logger = logging.getLogger("preseed")

DEFAULT_SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "seed_problems.json"


@dataclass
class SeedEntry:
    title: str
    raw_text: str


class SeedFileError(RuntimeError):
    """The seed file could not be read or understood."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _entry_from_mapping(row: dict, index: int) -> SeedEntry:
    raw_text = (row.get("raw_text") or "").strip()
    title = (row.get("title") or "").strip()
    body = (row.get("body") or "").strip()

    if raw_text:
        return SeedEntry(title=title or raw_text.splitlines()[0].strip(), raw_text=raw_text)
    if title and body:
        return SeedEntry(title=title, raw_text=f"{title}\n\n{body}")

    raise SeedFileError(
        f"entry {index}: need either 'raw_text', or both 'title' and 'body' "
        f"(got keys: {sorted(row)})"
    )


def load_entries(path: Path) -> list[SeedEntry]:
    """Read a JSON or CSV seed file into entries."""
    if not path.exists():
        raise SeedFileError(f"seed file not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SeedFileError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(payload, list):
            raise SeedFileError(f"{path} must contain a JSON list of objects")
        rows = payload
    elif suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter=delimiter))
        if not rows:
            raise SeedFileError(f"{path} has no data rows")
    else:
        raise SeedFileError(f"unsupported seed file type: {suffix} (use .json or .csv)")

    entries = [_entry_from_mapping(row, i) for i, row in enumerate(rows)]

    # Surface duplicates now rather than silently deduplicating later.
    seen: dict[str, str] = {}
    for entry in entries:
        digest = compute_hash(entry.raw_text)
        if digest in seen:
            logger.warning(
                "duplicate problem text: %r and %r hash identically",
                seen[digest],
                entry.title,
            )
        seen[digest] = entry.title

    return entries


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


@dataclass
class Totals:
    cached: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def processed(self) -> int:
        return self.cached + self.skipped + self.failed


def seed(entries: list[SeedEntry], *, dry_run: bool = False, delay: float = 0.0) -> Totals:
    settings = get_settings()
    totals = Totals()
    failures: list[tuple[str, str]] = []

    # Always ensure the schema: even a dry run reads the cache, so it can
    # report which entries are already covered. This is idempotent DDL and
    # writes no rows.
    create_db_and_tables()

    width = len(str(len(entries)))

    with Session(get_engine(settings)) as session:
        for position, entry in enumerate(entries, start=1):
            label = f"[{position:>{width}}/{len(entries)}] {entry.title}"

            if find_cached(session, entry.raw_text) is not None:
                totals.skipped += 1
                print(f"{label} - already cached, skipping")
                continue

            if dry_run:
                totals.skipped += 1
                print(f"{label} - would translate ({len(entry.raw_text)} chars)")
                continue

            try:
                result = translate_problem(entry.raw_text, settings=settings)
            except AllProvidersFailed as exc:
                totals.failed += 1
                failures.append((entry.title, str(exc)))
                print(f"{label} - FAILED: {exc}")
                continue

            store_translation(
                session,
                raw_text=entry.raw_text,
                problem=result.problem,
                is_preseeded=True,
            )
            totals.cached += 1
            print(f"{label} - cached via {result.provider.value}")

            # Pace the provider. Only after a real call; skips cost nothing.
            if delay and position < len(entries):
                time.sleep(delay)

    if failures:
        print("\nFailures:")
        for title, reason in failures:
            print(f"  - {title}: {reason}")

    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--file", type=Path, default=DEFAULT_SEED_FILE,
        help=f"JSON or CSV seed file (default: {DEFAULT_SEED_FILE.name})",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="only process the first N entries - useful for a trial run",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="parse the file and report what would happen; never calls the LLM",
    )
    parser.add_argument(
        "--delay", type=float, default=0.0,
        help="seconds to wait between LLM calls (default: 0)",
    )
    parser.add_argument("--verbose", action="store_true", help="show app log output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-8s %(name)s: %(message)s",
    )

    try:
        entries = load_entries(args.file)
    except SeedFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.limit is not None:
        entries = entries[: args.limit]

    mode = "DRY RUN - no LLM calls, nothing written" if args.dry_run else "seeding"
    print(f"{mode}: {len(entries)} problems from {args.file}\n")

    totals = seed(entries, dry_run=args.dry_run, delay=args.delay)

    print(
        f"\nDone. cached={totals.cached} skipped={totals.skipped} "
        f"failed={totals.failed} of {totals.processed}"
    )
    return 1 if totals.failed else 0


if __name__ == "__main__":
    sys.exit(main())
