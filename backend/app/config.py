"""Application configuration, loaded from environment variables / .env file."""

from enum import Enum
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderName(str, Enum):
    """The LLM providers this backend knows how to talk to."""

    GEMINI = "gemini"
    GROQ = "groq"


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
    groq_api_key: str = ""

    # --- Provider selection ---
    # The primary provider serves every translation. If it raises (network
    # error, auth failure, rate limit) or returns JSON that fails schema
    # validation, the fallback gets exactly one attempt. Set the fallback to an
    # empty string to disable failover entirely.
    llm_provider: ProviderName = ProviderName.GEMINI
    llm_fallback_provider: ProviderName | None = ProviderName.GROQ

    # --- Per-provider models ---
    # gemini-3.1-flash-lite: $0.25/1M in,  $1.50/1M out - cheapest Gemini tier.
    # openai/gpt-oss-120b:   $0.15/1M in,  $0.60/1M out, 131k ctx, on Groq.
    #   (openai/gpt-oss-20b at $0.075/$0.30 is the cheaper swap.)
    # Both support schema-constrained JSON output, which is what this needs.
    gemini_model: str = "gemini-3.1-flash-lite"
    groq_model: str = "openai/gpt-oss-120b"

    # Upper bound on a single translation's output. The JSON contract is small;
    # 2000 leaves generous headroom without letting a runaway response bill us.
    llm_max_tokens: int = 2000

    # Hard ceiling on a single provider call. Without this, a hung upstream
    # holds a worker thread until the platform's own timeout fires - and with
    # failover configured we would rather give up early and try the other one.
    llm_timeout_seconds: float = 45.0

    # --- Database (step 5) ---
    database_url: str = ""

    # --- Quota ---
    free_call_limit: int = 5

    # --- Abuse / cost protection ---
    # `install_id` is client-generated and unauthenticated, so the per-install
    # quota alone is trivially reset by minting a new UUID. These are the
    # backstop that makes /translate safe to expose publicly.
    rate_limits_enabled: bool = True

    # Every request from one IP, per hour. Generous - a volumetric brake that
    # should never bite a real user browsing cached problems.
    rate_limit_requests_per_hour: int = 120

    # Cache *misses* from one IP, per hour. This is the real cost brake, since
    # only a miss reaches a provider.
    rate_limit_llm_per_hour: int = 20

    # New install_ids registered from one IP, per hour. This is what closes the
    # quota-reset vector: fresh UUIDs stop being free after a few.
    rate_limit_new_installs_per_hour: int = 3

    # Hard ceiling on LLM calls per day across every user. When this trips the
    # service stays up in cache-only mode rather than continuing to spend.
    # At current pricing ~1000 calls is roughly $0.50-0.75/day.
    llm_daily_cap: int = 1000

    # --- HTTP ---
    cors_allow_origins: str = "*"

    # --- Observability ---
    # Empty disables Sentry entirely; the app runs fine without it.
    sentry_dsn: str = ""
    sentry_environment: str = "development"
    # Fraction of requests traced for performance data. 1.0 is fine at low
    # volume; lower it if the Sentry quota becomes the constraint.
    sentry_traces_sample_rate: float = 0.1

    # Off by default on purpose. Enabling this sends request headers and client
    # IPs to Sentry, which changes what the privacy policy has to disclose -
    # the SRS commits to collecting only an anonymous install ID and the pasted
    # problem text. Pasted text is scrubbed from error payloads either way.
    sentry_send_pii: bool = False

    # --- Admin dashboard ---
    # Bearer token for /admin. Empty disables the dashboard entirely, which is
    # the safe default: an unset token must never mean "no auth required".
    admin_token: str = ""

    # --- Background jobs ---
    enable_scheduler: bool = True

    # Hour (UTC) at which the daily-problem job runs. A fixed wall-clock time
    # rather than an interval, so redeploys don't keep resetting the countdown.
    daily_job_hour: int = 3

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
            ProviderName.GROQ: self.groq_api_key,
        }[provider]

    def model_for(self, provider: ProviderName) -> str:
        """The configured model id for a provider."""
        return {
            ProviderName.GEMINI: self.gemini_model,
            ProviderName.GROQ: self.groq_model,
        }[provider]


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Use this everywhere instead of Settings()."""
    return Settings()
