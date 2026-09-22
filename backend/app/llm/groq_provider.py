"""Groq (GroqCloud) adapter — fallback provider.

Groq serves open models on custom inference hardware; the API is
OpenAI-compatible and reached through the official `groq` SDK.

The default model, `openai/gpt-oss-120b`, supports Groq's strict structured
output mode (`strict: true`), which enforces the JSON schema server-side. We
still take only the raw text off the response and validate it in service.py, so
Groq output is held to exactly the same standard as Gemini output.
"""

import logging

from app.config import ProviderName, Settings
from app.llm.base import ProviderCallFailed, ProviderNotConfigured
from app.schemas import groq_json_schema

logger = logging.getLogger(__name__)

# Groq's strict mode is only guaranteed on some models. On anything else the
# request still succeeds, but schema adherence is best-effort - which our own
# validation gate then catches.
_STRICT_CAPABLE_PREFIXES = ("openai/gpt-oss-", "qwen")


class GroqProvider:
    """Talks to Groq. Returns raw text; validation happens in service.py."""

    name = ProviderName.GROQ

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = settings.model_for(ProviderName.GROQ)
        api_key = settings.api_key_for(ProviderName.GROQ)
        if not api_key:
            raise ProviderNotConfigured(
                "GROQ_API_KEY is not set - get one at https://console.groq.com/keys"
            )
        # Imported lazily so the app can boot on a machine where the provider
        # SDKs are not importable.
        from groq import Groq

        # Give up rather than hold a worker on a hung upstream.
        self._client = Groq(api_key=api_key, timeout=settings.llm_timeout_seconds)

    @property
    def _supports_strict(self) -> bool:
        return self._model.lower().startswith(_STRICT_CAPABLE_PREFIXES)

    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        try:
            completion = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_completion_tokens=self._settings.llm_max_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "simplified_problem",
                        "schema": groq_json_schema(),
                        "strict": self._supports_strict,
                    },
                },
                # Restating a problem is rewriting, not reasoning. Keeping this
                # low avoids paying for reasoning tokens on a mechanical task.
                reasoning_effort="low",
                # Deterministic-ish output makes the cache more useful: two
                # users pasting the same problem get the same restatement.
                temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001 - normalised into our own type
            raise ProviderCallFailed(f"Groq request failed: {exc}") from exc

        if not completion.choices:
            raise ProviderCallFailed("Groq returned no choices")

        choice = completion.choices[0]
        text = choice.message.content

        if not text or not text.strip():
            raise ProviderCallFailed(
                f"Groq returned no text (finish_reason={choice.finish_reason!r})"
            )

        usage = completion.usage
        logger.info(
            "groq translation ok model=%s finish=%s in_tokens=%s out_tokens=%s",
            self._model,
            choice.finish_reason,
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
        )
        return text
