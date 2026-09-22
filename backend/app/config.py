"""Application configuration, loaded from environment variables / .env file."""

from enum import Enum
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderName(str, Enum):
    """The LLM providers this backend knows how to talk to."""

    GEMINI = "gemini"
    GROK = "grok"


class Settings(BaseSettings):
    """All runtime configuration for the LeetDecode backend.

    Values are read from the process environment first, then from a local
    `.env` file (which is gitignored). Unknown env vars are ignored so that
    platform-injected variables (Railway's PORT, RAILWAY_*, ...) don't blow up
    startup.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Provider credentials ---
    # Empty defaults so the app can boot (and /health can answer) before these
    # are set. Each adapter validates its own key at call time, not at import.
    gemini_api_key: str = ""
    xai_api_key: str = ""

    # --- Provider selection ---
    # The primary provider serves every translation. If it raises (network
    # error, auth failure, rate limit) or returns JSON that fails schema
    # validation, the fallback gets exactly one attempt. Set the fallback to an
    # empty string to disable failover entirely.
    llm_provider: ProviderName = ProviderName.GEMINI
    llm_fallback_provider: ProviderName | None = ProviderName.GROK

    # --- Per-provider models ---
    # gemini-3.1-flash-lite: $0.25/1M in, $1.50/1M out - cheapest current tier.
    # grok-4.3:              $1.25/1M in, $2.50/1M out, 1M context.
    # Both are plain rewriting workloads, so the cheapest tier is the right fit.
    gemini_model: str = "gemini-3.1-flash-lite"
    grok_model: str = "grok-4.3"

    # Upper bound on a single translation's output. The JSON contract is small;
    # 2000 leaves generous headroom without letting a runaway response bill us.
    llm_max_tokens: int = 2000

    # --- Database (step 5) ---
    database_url: str = ""

    # --- Quota ---
    free_call_limit: int = 5

    # --- HTTP ---
    cors_allow_origins: str = "*"

    # --- Background jobs ---
    enable_scheduler: bool = True

    @field_validator("llm_fallback_provider", mode="before")
    @classmethod
    def _blank_fallback_means_disabled(cls, v: object) -> object:
        """Treat LLM_FALLBACK_PROVIDER="" as "no fallback" rather than an error."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS_ALLOW_ORIGINS as a list, e.g. "*" or "a.com,b.com"."""
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    def api_key_for(self, provider: ProviderName) -> str:
        """The configured credential for a provider (may be empty)."""
        return {
            ProviderName.GEMINI: self.gemini_api_key,
            ProviderName.GROK: self.xai_api_key,
        }[provider]

    def model_for(self, provider: ProviderName) -> str:
        """The configured model id for a provider."""
        return {
            ProviderName.GEMINI: self.gemini_model,
            ProviderName.GROK: self.grok_model,
        }[provider]


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Use this everywhere instead of Settings()."""
    return Settings()
