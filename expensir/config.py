"""Deployment configuration via environment / .env (§14)."""

from typing import Literal, Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mode: Literal["webhook", "poll"] = "webhook"
    bot_token: str
    telegram_webhook_secret: str
    public_url: str | None = None  # for setWebhook (prod)
    database_url: str
    operator_user_id: int | None = None
    undo_window_hours: int = 24  # after this, undo/redo is operator-only (§9)
    telegram_api_base: str = "https://api.telegram.org"  # overridable for stub/test servers
    # the NL extractor endpoint (ADR-0010): any OpenAI-compatible provider —
    # Cloudflare Workers AI, OpenRouter, DigitalOcean, Groq. Unset = NL disabled.
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None  # provider-specific id; verify in the dashboard (§1)
    pending_ttl_minutes: int = 15  # proposal TTL (§10, §17)

    @field_validator(
        "public_url", "operator_user_id", "llm_base_url", "llm_api_key", "llm_model", mode="before"
    )
    @classmethod
    def _blank_env_value_means_unset(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _webhook_mode_needs_a_secret(self) -> Self:
        # an empty secret would let any forged request with an empty header through (§13)
        if self.mode == "webhook" and not self.telegram_webhook_secret:
            raise ValueError("TELEGRAM_WEBHOOK_SECRET must be set when MODE=webhook")
        return self
