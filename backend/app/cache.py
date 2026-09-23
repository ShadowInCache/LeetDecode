"""Cache key derivation and lookup for `problems_cache`.

Two lookups, in order:

1. **Hash match.** SHA-256 of the normalized text. Exact, and the common case
   when two users copy the same problem the same way.
2. **Title fallback.** The first non-empty line. Users paste inconsistently -
   they include the difficulty tag, or drop the constraints, or copy the
   examples in a different order - and all of those change the hash while still
   being the same problem. Matching on title recovers those hits.

The fallback is restricted to preseeded rows on purpose. Preseeded titles are
curated and known-good; organically cached rows carry whatever first line the
original paster happened to have, which is not trustworthy enough to serve to a
different user on a title match alone.
"""

import hashlib
import logging
import re
import uuid

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import ProblemCache, utcnow
from app.schemas import SimplifiedProblem

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")
# Leading LeetCode numbering ("1. Two Sum", "#1 Two Sum") and trailing
# difficulty tags, which vary by how the user selected the text.
# Either a "#1 " prefix (no delimiter needed) or "1. " / "42) " / "7 - ".
_LEADING_NUMBER_RE = re.compile(r"^\s*(?:#\s*\d+\s+|\d+\s*[.\-)]\s*)")
_TRAILING_DIFFICULTY_RE = re.compile(r"\s*[-–|]?\s*(easy|medium|hard)\s*$", re.IGNORECASE)

MAX_TITLE_LENGTH = 512


def normalize_text(raw_text: str) -> str:
    """Collapse the incidental differences between two pastes of one problem.

    Lowercased, whitespace-collapsed, trimmed. Deliberately conservative: it
    does not strip punctuation, because `[]` vs `()` can be load-bearing in a
    problem statement.
    """
    return _WHITESPACE_RE.sub(" ", raw_text.strip().lower()).strip()


def compute_hash(raw_text: str) -> str:
    """SHA-256 of the normalized text, as hex."""
    return hashlib.sha256(normalize_text(raw_text).encode("utf-8")).hexdigest()


def extract_title(raw_text: str) -> str:
    """Best guess at the problem title: the first line with content in it.

    Strips LeetCode's leading number and trailing difficulty tag so that
    "1. Two Sum", "Two Sum - Easy" and "Two Sum" all normalize together.
    """
    for line in raw_text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        candidate = _LEADING_NUMBER_RE.sub("", candidate)
        candidate = _TRAILING_DIFFICULTY_RE.sub("", candidate)
        candidate = candidate.strip()
        if candidate:
            return candidate[:MAX_TITLE_LENGTH]
    return ""


def normalize_title(title: str) -> str:
    """Comparison form for titles: lowercased and whitespace-collapsed."""
    return _WHITESPACE_RE.sub(" ", title.strip().lower()).strip()


def find_cached(session: Session, raw_text: str) -> ProblemCache | None:
    """Look for an existing translation: hash first, then preseeded title.

    A pure read. `last_used_at` is *not* updated here: this runs on the hot
    path, and a write plus commit plus refresh added three round trips to a
    request that should need one. `touch_last_used()` does it afterwards, off
    the response path. Usage counters are never touched - a cache hit costs no
    quota.
    """
    problem_hash = compute_hash(raw_text)
    row = session.exec(
        select(ProblemCache).where(ProblemCache.problem_hash == problem_hash)
    ).first()

    if row is None:
        title = normalize_title(extract_title(raw_text))
        if title:
            row = session.exec(
                select(ProblemCache)
                .where(func.lower(ProblemCache.problem_title) == title)
                .where(ProblemCache.is_preseeded.is_(True))
            ).first()
            if row is not None:
                logger.info("cache hit via title fallback title=%r", title)
    else:
        logger.info("cache hit via hash")

    return row


def touch_last_used(session: Session, row_id: uuid.UUID) -> None:
    """Record that a cached row was served. Safe to run after the response.

    A bare UPDATE by primary key: no SELECT, no ORM identity-map round trip.
    The column feeds analytics and any future LRU eviction, so it is worth
    keeping accurate - but never at the cost of the user's latency.
    """
    session.execute(
        update(ProblemCache)
        .where(ProblemCache.id == row_id)
        .values(last_used_at=utcnow())
    )
    session.commit()


def store_translation(
    session: Session,
    *,
    raw_text: str,
    problem: SimplifiedProblem,
    is_preseeded: bool = False,
) -> ProblemCache:
    """Insert a validated translation, or return the existing row on conflict.

    `problem` is a `SimplifiedProblem`, not a dict, so it is impossible to reach
    this function with unvalidated model output.
    """
    problem_hash = compute_hash(raw_text)

    existing = session.exec(
        select(ProblemCache).where(ProblemCache.problem_hash == problem_hash)
    ).first()
    if existing is not None:
        return existing

    row = ProblemCache(
        problem_hash=problem_hash,
        problem_title=extract_title(raw_text),
        simplified_json=problem.model_dump(mode="json"),
        is_preseeded=is_preseeded,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # Another request cached the same problem between our SELECT above and
        # this INSERT. `problem_hash` is unique, so one writer wins and the
        # other lands here. That is a cache hit, not an error - roll back and
        # return the row that won.
        session.rollback()
        winner = session.exec(
            select(ProblemCache).where(ProblemCache.problem_hash == problem_hash)
        ).first()
        if winner is not None:
            logger.info("concurrent insert for %r - using the stored row", problem_hash[:12])
            return winner
        # The constraint fired for some other reason; don't swallow it.
        raise

    session.refresh(row)
    logger.info("cached translation preseeded=%s title=%r", is_preseeded, row.problem_title)
    return row
