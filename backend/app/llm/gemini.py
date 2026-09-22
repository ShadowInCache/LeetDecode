"""Google Gemini adapter (primary provider).

Uses the Interactions API: `client.interactions.create(...)` returning an
`Interaction` whose `output_text` holds the response. JSON mode is requested via
`response_format`, with the schema from `app.schemas.gemini_json_schema()`.
"""

import logging

from app.config import ProviderName, Settings
from app.llm.base import ProviderCallFailed, ProviderNotConfigured
from app.schemas import gemini_json_schema

logger = logging.getLogger(__name__)


class GeminiProvider:
    """Talks to Gemini. Returns raw text; validation happens in service.py."""

    name = ProviderName.GEMINI

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = settings.model_for(ProviderName.GEMINI)
        api_key = settings.api_key_for(ProviderName.GEMINI)
        if not api_key:
            raise ProviderNotConfigured(
                "GEMINI_API_KEY is not set - get one at https://aistudio.google.com/apikey"
            )
        # Imported lazily so the app can boot (and /health answer) on a machine
        # where the provider SDKs are not importable.
        from google import genai

        self._client = genai.Client(api_key=api_key)

    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        try:
            interaction = self._client.interactions.create(
                model=self._model,
                input=user_prompt,
                system_instruction=system_prompt,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": gemini_json_schema(),
                },
                generation_config={
                    "max_output_tokens": self._settings.llm_max_tokens,
                    # Restating a problem is a rewriting task, not a reasoning
                    # one. Minimal thinking keeps latency and cost down.
                    "thinking_level": "minimal",
                },
                # Give up rather than hold a worker on a hung upstream; with a
                # fallback configured we would rather fail over quickly.
                timeout=self._settings.llm_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - normalised into our own type
            raise ProviderCallFailed(f"Gemini request failed: {exc}") from exc

        text = interaction.output_text
        if not text or not text.strip():
            # A blank body with a non-failed status usually means the response
            # was cut short or filtered; surface the status so logs are useful.
            raise ProviderCallFailed(
                f"Gemini returned no text (status={interaction.status!r})"
            )

        logger.info(
            "gemini translation ok model=%s status=%s chars=%d",
            self._model,
            interaction.status,
            len(text),
        )
        return text
