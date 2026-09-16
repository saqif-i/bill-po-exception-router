"""Configuration, validated at startup.

Startup runs before the first request is accepted and fails the process on any
invalid setting (Volume 02 section 9.2). An unknown write mode is a hard failure
rather than a warning, because failing closed is the point (I08).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WriteMode(StrEnum):
    """v1 supports only `disabled`. See ADR-006."""

    DISABLED = "disabled"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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

    # Default off (Volume 08 section 9.2). Part 7 turns it on, and only after
    # the provider questions in docs/security.md have been answered.
    semantic_review_enabled: bool = Field(default=False, alias="SEMANTIC_REVIEW_ENABLED")

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


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
