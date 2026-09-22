"""Provider selection, failover, and the single validation gate.

Every translation in the system - `/translate`, the daily-problem job, the
pre-seed script - goes through `translate_problem()`. That is deliberate: it is
the one place a provider response is checked against `SimplifiedProblem`, so
nothing can reach `problems_cache` without having passed.
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

from pydantic import ValidationError

from app.config import ProviderName, Settings, get_settings
from app.llm.base import (
    AllProvidersFailed,
    InvalidLLMOutput,
    LLMProvider,
    ProviderNotConfigured,
)
from app.llm.gemini import GeminiProvider
from app.llm.groq_provider import GroqProvider
from app.llm.prompt import SYSTEM_PROMPT, build_user_prompt
from app.schemas import SimplifiedProblem

logger = logging.getLogger(__name__)

# Both providers are asked for JSON mode, so fences should never appear. This is
# belt-and-braces for the case where a model wraps its output anyway - stripping
# a wrapper is a transport concern, not a schema concession. The payload inside
# still has to validate.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

PROVIDER_FACTORIES: dict[ProviderName, Callable[[Settings], LLMProvider]] = {
    ProviderName.GEMINI: GeminiProvider,
    ProviderName.GROQ: GroqProvider,
}


@dataclass(frozen=True)
class TranslationResult:
    """A validated translation, plus what it took to produce it."""

    problem: SimplifiedProblem
    provider: ProviderName
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None


def _strip_fences(text: str) -> str:
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text.strip()


def validate_output(raw_text: str) -> SimplifiedProblem:
    """Parse and validate a provider response, or raise `InvalidLLMOutput`.

    This is the gate. Unparseable JSON and schema-violating JSON both land here
    as the same failure, because both mean the same thing downstream: there is
    nothing safe to cache or show the user.
    """
    candidate = _strip_fences(raw_text)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise InvalidLLMOutput(f"response was not valid JSON: {exc}") from exc

    try:
        return SimplifiedProblem.model_validate(payload)
    except ValidationError as exc:
        # Report the field-level problems; the raw text may be long and is
        # already logged at debug level by the caller if needed.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        raise InvalidLLMOutput(f"response did not match the schema - {problems}") from exc


def _provider_chain(settings: Settings) -> list[ProviderName]:
    """Primary first, then the fallback if one is configured and distinct."""
    chain = [settings.llm_provider]
    fallback = settings.llm_fallback_provider
    if fallback is not None and fallback != settings.llm_provider:
        chain.append(fallback)
    return chain


def translate_problem(
    raw_text: str, *, settings: Settings | None = None
) -> TranslationResult:
    """Turn pasted problem text into a validated `SimplifiedProblem`.

    Tries the primary provider, then the fallback. A provider is considered
    failed for *either* a transport error or schema-invalid output - a model
    that ignores the contract is as useless as one that is down, and the whole
    point of having a second provider is to survive both.

    Raises:
        AllProvidersFailed: no configured provider produced a valid result.
    """
    settings = settings or get_settings()
    user_prompt = build_user_prompt(raw_text)
    failures: dict[ProviderName, Exception] = {}

    for provider_name in _provider_chain(settings):
        try:
            provider = PROVIDER_FACTORIES[provider_name](settings)
        except ProviderNotConfigured as exc:
            logger.warning("provider %s skipped: %s", provider_name.value, exc)
            failures[provider_name] = exc
            continue

        started = time.monotonic()
        try:
            response = provider.generate(
                system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt
            )
            problem = validate_output(response.text)
        except Exception as exc:  # noqa: BLE001 - recorded, then we try the next one
            logger.warning(
                "provider %s failed: %s: %s", provider_name.value, type(exc).__name__, exc
            )
            failures[provider_name] = exc
            continue

        latency_ms = int((time.monotonic() - started) * 1000)

        if failures:
            logger.info(
                "translation recovered on fallback provider=%s", provider_name.value
            )
        return TranslationResult(
            problem=problem,
            provider=provider_name,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=latency_ms,
        )

    raise AllProvidersFailed(failures)
