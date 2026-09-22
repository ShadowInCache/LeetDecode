"""Daily LeetCode problem pre-caching.

`refresh_daily_problem()` is a standalone function on purpose: the scheduler
calls it on a timer, but you can also run it by hand (see
`scripts/run_daily_job.py`) to test it without waiting 24 hours.
"""

import logging
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx
from sqlmodel import Session

from app import ratelimit
from app.cache import find_cached, store_translation
from app.config import Settings, get_settings
from app.db import get_engine
from app.llm import AllProvidersFailed, translate_problem

logger = logging.getLogger(__name__)

LEETCODE_GRAPHQL_URL = "https://leetcode.com/graphql"

DAILY_QUERY = """
query questionOfToday {
  activeDailyCodingChallengeQuestion {
    date
    link
    question {
      title
      titleSlug
      difficulty
      content
    }
  }
}
"""

REQUEST_TIMEOUT = 20.0


class DailyProblemError(RuntimeError):
    """The daily problem could not be fetched or parsed."""


@dataclass(frozen=True)
class DailyProblem:
    """The daily challenge, reduced to what we need."""

    date: str
    title: str
    slug: str
    difficulty: str
    content: str

    def as_raw_text(self) -> str:
        """Render as the kind of text a user would paste.

        Matching the user-paste shape matters: the cache key is a hash of this
        text, and the title fallback reads its first line. Producing the same
        shape is what lets a user's paste of the daily problem hit this row.
        """
        return f"{self.title}\n\n{self.content}"


class _HTMLToText(HTMLParser):
    """Flatten LeetCode's HTML question body into plain text.

    LeetCode returns `content` as HTML. Feeding raw markup to the model wastes
    tokens and invites it to echo tags, so we strip to text while keeping block
    structure as line breaks.
    """

    _BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "pre", "h1", "h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        # Collapse runs of blank lines left behind by nested block tags.
        lines = [line.strip() for line in joined.splitlines()]
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    parser = _HTMLToText()
    parser.feed(html)
    return parser.text()


def fetch_daily_problem(timeout: float = REQUEST_TIMEOUT) -> DailyProblem:
    """Fetch today's challenge from LeetCode's public GraphQL endpoint."""
    try:
        response = httpx.post(
            LEETCODE_GRAPHQL_URL,
            json={"query": DAILY_QUERY, "operationName": "questionOfToday"},
            headers={
                "Content-Type": "application/json",
                # LeetCode rejects requests without a browser-ish UA.
                "User-Agent": "Mozilla/5.0 (compatible; LeetDecode/0.1)",
                "Referer": "https://leetcode.com/",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        raise DailyProblemError(f"LeetCode request failed: {exc}") from exc
    except ValueError as exc:
        raise DailyProblemError(f"LeetCode returned non-JSON: {exc}") from exc

    if body.get("errors"):
        raise DailyProblemError(f"LeetCode GraphQL errors: {body['errors']}")

    try:
        node = body["data"]["activeDailyCodingChallengeQuestion"]
        question = node["question"]
        content = question["content"]
    except (KeyError, TypeError) as exc:
        raise DailyProblemError(f"unexpected GraphQL response shape: {exc}") from exc

    if not content:
        # Premium-only problems come back with a null body.
        raise DailyProblemError("daily problem has no public content")

    return DailyProblem(
        date=node.get("date", ""),
        title=question["title"],
        slug=question["titleSlug"],
        difficulty=question.get("difficulty", ""),
        content=html_to_text(content),
    )


def refresh_daily_problem(settings: Settings | None = None) -> bool:
    """Fetch, translate and cache today's problem. Returns True if cached.

    Safe to run repeatedly: if today's problem is already cached the LLM is
    never called. Never raises - it runs unattended on a timer, and a failed
    refresh must not take the scheduler thread down with it.
    """
    settings = settings or get_settings()

    try:
        daily = fetch_daily_problem()
    except DailyProblemError as exc:
        logger.error("daily job: could not fetch problem: %s", exc)
        return False

    raw_text = daily.as_raw_text()

    try:
        with Session(get_engine(settings)) as session:
            # Housekeeping: closed rate-limit windows are dead weight.
            ratelimit.purge_expired(session)

            if find_cached(session, raw_text) is not None:
                logger.info("daily job: %r already cached, skipping", daily.title)
                return True

            try:
                result = translate_problem(raw_text, settings=settings)
            except AllProvidersFailed as exc:
                logger.error("daily job: translation failed for %r: %s", daily.title, exc)
                return False

            store_translation(
                session,
                raw_text=raw_text,
                problem=result.problem,
                is_preseeded=True,  # always free, like the curated set
            )
    except Exception as exc:  # noqa: BLE001 - unattended job, must not crash
        logger.exception("daily job: unexpected failure: %s", exc)
        return False

    logger.info(
        "daily job: cached %r (%s) via %s", daily.title, daily.date, result.provider.value
    )
    return True
