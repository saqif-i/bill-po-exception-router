"""Configuration, validated at startup.

Startup runs before the first request is accepted and fails the process on any
invalid setting. An unknown write mode, or semantic review enabled without an
API key, is a hard failure rather than a warning, because failing closed is the
point (I08).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WriteMode(StrEnum):
    """v1 supports only `disabled`. See ADR-006."""

    DISABLED = "disabled"


class Settings(BaseSettings):
    # An empty variable counts as unset. Compose passes optional settings as
    # `${VAR:-}`, which would otherwise override a default with an empty string.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # Runtime database role. Never the owner, never the bootstrap superuser.
    bpr_database_url: str = Field(alias="BPR_DATABASE_URL")
    internal_bearer_token: str = Field(alias="INTERNAL_BEARER_TOKEN", min_length=32)
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    service_port: int = Field(default=8000, alias="SERVICE_PORT")

    # I08: unset resolves to disabled; an unknown value refuses to start.
    xero_write_mode: str = Field(default="disabled", alias="XERO_WRITE_MODE")

    # Xero Custom Connection. Optional so the service still starts for unit
    # tests and for anyone who has not connected a provider yet.
    xero_client_id: str | None = Field(default=None, alias="XERO_CLIENT_ID")
    xero_client_secret: str | None = Field(default=None, alias="XERO_CLIENT_SECRET")
    xero_scopes: str = Field(
        default="accounting.transactions.read accounting.settings.read accounting.contacts.read",
        alias="XERO_SCOPES",
    )
    # Live tests refuse to run against anything that is not this organisation.
    xero_expected_org_name: str = Field(default="Demo Company", alias="XERO_EXPECTED_ORG_NAME")
    xero_connect_timeout_seconds: float = Field(default=10.0, alias="XERO_CONNECT_TIMEOUT_SECONDS")
    xero_read_timeout_seconds: float = Field(default=30.0, alias="XERO_READ_TIMEOUT_SECONDS")
    xero_max_attempts: int = Field(default=4, alias="XERO_MAX_ATTEMPTS")

    # Default off. Turn it on only after
    # the provider questions in docs/security.md have been answered.
    semantic_review_enabled: bool = Field(default=False, alias="SEMANTIC_REVIEW_ENABLED")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # Haiku 4.5 is the low-cost tier for classification and extraction, which is
    # exactly this task: one question about two short strings, with verbatim
    # evidence attached. It is also the only current id carrying a DATE, so
    # pinning it actually pins it. Move up to claude-sonnet-5 only if your own
    # evaluation gives you a reason (evaluations/run_eval.py).
    semantic_model_id: str = Field(default="claude-haiku-4-5-20251001", alias="SEMANTIC_MODEL_ID")
    semantic_min_confidence: float = Field(default=0.6, alias="SEMANTIC_MIN_CONFIDENCE")
    semantic_timeout_seconds: float = Field(default=30.0, alias="SEMANTIC_TIMEOUT_SECONDS")

    # Slack. Optional, so the service starts without a Slack app configured.
    slack_bot_token: str | None = Field(default=None, alias="SLACK_BOT_TOKEN")
    slack_signing_secret: str | None = Field(default=None, alias="SLACK_SIGNING_SECRET")

    @field_validator("xero_write_mode")
    @classmethod
    def _write_mode_fails_closed(cls, value: str) -> str:
        normalised = (value or "disabled").strip().lower()
        if normalised not in {mode.value for mode in WriteMode}:
            raise ValueError(
                f"XERO_WRITE_MODE={value!r} is not a supported value. "
                f"v1 is read-only; the only supported value is 'disabled'."
            )
        return normalised

    @field_validator("bpr_database_url")
    @classmethod
    def _runtime_role_only(cls, value: str) -> str:
        """The service must not hold DDL authority or the bootstrap credential."""
        forbidden = ("bpr_owner", "postgres@", "//postgres:")
        if any(token in value for token in forbidden):
            raise ValueError(
                "BPR_DATABASE_URL must use the bpr_app runtime role. "
                "The owner and bootstrap credentials are never given to the service."
            )
        return value

    @model_validator(mode="after")
    def _semantic_review_needs_a_key(self) -> Settings:
        """Fails closed like the write mode: enabling review without a key would
        send every call to fail as SEMANTIC_PROVIDER_UNAVAILABLE."""
        if self.semantic_review_enabled and not self.anthropic_api_key:
            raise ValueError("SEMANTIC_REVIEW_ENABLED=true requires ANTHROPIC_API_KEY to be set.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
