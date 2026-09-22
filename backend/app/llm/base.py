"""Provider-agnostic interface and error types for LLM translation."""

from typing import Protocol, runtime_checkable

from app.config import ProviderName


class LLMError(RuntimeError):
    """Base class for every failure raised by the llm package."""


class ProviderNotConfigured(LLMError):
    """The provider is selected but has no API key set."""


class ProviderCallFailed(LLMError):
    """The provider was reachable but the call failed (auth, rate limit, 5xx, timeout)."""


class InvalidLLMOutput(LLMError):
    """The provider returned something that is not a valid `SimplifiedProblem`.

    Raised for unparseable JSON and for JSON that parses but violates the
    schema. Per the SRS, a response in this state is never cached and never
    returned - it surfaces as an error.
    """


class AllProvidersFailed(LLMError):
    """Every configured provider (primary, then fallback) failed.

    Carries the per-provider causes so the log line explains what actually went
    wrong rather than just "translation failed".
    """

    def __init__(self, failures: dict[ProviderName, Exception]) -> None:
        self.failures = failures
        detail = "; ".join(
            f"{name.value}: {type(exc).__name__}: {exc}" for name, exc in failures.items()
        )
        super().__init__(f"all providers failed - {detail}")


@runtime_checkable
class LLMProvider(Protocol):
    """One LLM backend.

    Implementations do exactly one thing: send the two prompts and hand back the
    model's raw text. They do not parse or validate it. Validation lives in
    `service.py` so that every provider is held to an identical standard - which
    is what makes the cache trustworthy regardless of which backend filled it.
    """

    name: ProviderName

    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        """Return the model's raw response text, expected to be JSON.

        Raises:
            ProviderNotConfigured: no credential available.
            ProviderCallFailed: the request did not produce usable text.
        """
        ...
