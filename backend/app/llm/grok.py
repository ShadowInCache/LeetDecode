"""xAI Grok adapter (fallback provider).

`xai-sdk` accepts a Pydantic model class directly as `response_format`, so it
builds the JSON schema itself - no hand-rolled schema needed here. We still take
only the raw text off the response and let service.py validate it, so Grok output
is held to exactly the same standard as Gemini output.
"""

import logging

from app.config import ProviderName, Settings
from app.llm.base import ProviderCallFailed, ProviderNotConfigured
from app.schemas import SimplifiedProblem

logger = logging.getLogger(__name__)


class GrokProvider:
    """Talks to Grok. Returns raw text; validation happens in service.py."""

    name = ProviderName.GROK

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = settings.model_for(ProviderName.GROK)
        api_key = settings.api_key_for(ProviderName.GROK)
        if not api_key:
            raise ProviderNotConfigured(
                "XAI_API_KEY is not set - get one at https://console.x.ai/"
            )
        # Imported lazily so the app can boot on a machine where the provider
        # SDKs are not importable.
        from xai_sdk import Client

        self._client = Client(api_key=api_key)

    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        from xai_sdk.chat import system as system_msg
        from xai_sdk.chat import user as user_msg

        try:
            chat = self._client.chat.create(
                model=self._model,
                max_tokens=self._settings.llm_max_tokens,
                # The SDK derives the JSON schema from the Pydantic class.
                response_format=SimplifiedProblem,
            )
            chat.append(system_msg(system_prompt))
            chat.append(user_msg(user_prompt))
            response = chat.sample()
        except Exception as exc:  # noqa: BLE001 - normalised into our own type
            raise ProviderCallFailed(f"Grok request failed: {exc}") from exc

        text = response.content
        if not text or not text.strip():
            raise ProviderCallFailed(
                f"Grok returned no text (finish_reason={response.finish_reason!r})"
            )

        logger.info(
            "grok translation ok model=%s finish=%s chars=%d",
            self._model,
            response.finish_reason,
            len(text),
        )
        return text
