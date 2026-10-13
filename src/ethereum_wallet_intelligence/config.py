"""Application configuration."""

from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated settings loaded from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_name: str = Field(
        default="Ethereum Wallet Intelligence",
        min_length=1,
        validation_alias="EWI_APP_NAME",
    )
    app_host: str = Field(default="127.0.0.1", min_length=1, validation_alias="EWI_APP_HOST")
    app_port: int = Field(default=8000, ge=1, le=65535, validation_alias="EWI_APP_PORT")
    ethereum_rpc_url: SecretStr | None = Field(default=None, validation_alias="ETHEREUM_RPC_URL")
    ethereum_rpc_timeout_seconds: float = Field(
        default=10.0,
        ge=0.1,
        le=60.0,
        validation_alias="ETHEREUM_RPC_TIMEOUT_SECONDS",
    )
    ethereum_rpc_max_attempts: int = Field(
        default=3,
        ge=1,
        le=5,
        validation_alias="ETHEREUM_RPC_MAX_ATTEMPTS",
    )
    ethereum_rpc_retry_base_delay_seconds: float = Field(
        default=0.25,
        ge=0,
        le=5,
        validation_alias="ETHEREUM_RPC_RETRY_BASE_DELAY_SECONDS",
    )
    ethereum_rpc_retry_max_delay_seconds: float = Field(
        default=2.0,
        ge=0,
        le=30,
        validation_alias="ETHEREUM_RPC_RETRY_MAX_DELAY_SECONDS",
    )
    ethereum_rpc_retry_jitter_ratio: float = Field(
        default=0.2,
        ge=0,
        le=1,
        validation_alias="ETHEREUM_RPC_RETRY_JITTER_RATIO",
    )
    database_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL")

    @model_validator(mode="after")
    def validate_rpc_retry_delays(self) -> Self:
        """Keep the maximum retry delay at or above its base delay."""
        if self.ethereum_rpc_retry_max_delay_seconds < self.ethereum_rpc_retry_base_delay_seconds:
            raise ValueError("Ethereum RPC retry maximum delay cannot be below its base delay")
        return self

    @field_validator("ethereum_rpc_url")
    @classmethod
    def validate_ethereum_rpc_url(cls, value: SecretStr | None) -> SecretStr | None:
        """Require an HTTP(S) endpoint without exposing credentials in validation output."""
        if value is None:
            return None

        raw_value = value.get_secret_value().strip()
        parsed = urlsplit(raw_value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("Ethereum RPC URL must be a valid HTTP(S) URL")
        return SecretStr(raw_value)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        """Require a PostgreSQL asyncpg endpoint without exposing credentials in error output."""
        if value is None:
            return None

        raw_value = value.get_secret_value().strip()
        parsed = urlsplit(raw_value)
        if (
            parsed.scheme not in {"postgresql+asyncpg", "postgres+asyncpg"}
            or parsed.hostname is None
        ):
            raise ValueError(
                "DATABASE_URL must be a valid PostgreSQL asyncpg URL "
                "(e.g., postgresql+asyncpg://user:pass@host:port/dbname)"
            )
        return SecretStr(raw_value)

    def require_ethereum_rpc_url(self) -> str:
        """Return configured RPC URL or fail when an RPC-dependent component needs it."""
        if self.ethereum_rpc_url is None:
            raise ValueError("ETHEREUM_RPC_URL is required for Ethereum RPC access")
        return self.ethereum_rpc_url.get_secret_value()

    def require_database_url(self) -> str:
        """Return configured database URL or fail when a persistence component needs it."""
        if self.database_url is None:
            raise ValueError("DATABASE_URL is required for database persistence")
        return self.database_url.get_secret_value()
