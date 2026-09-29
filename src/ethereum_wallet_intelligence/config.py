"""Application configuration."""

from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
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

    def require_ethereum_rpc_url(self) -> str:
        """Return configured RPC URL or fail when an RPC-dependent component needs it."""
        if self.ethereum_rpc_url is None:
            raise ValueError("ETHEREUM_RPC_URL is required for Ethereum RPC access")
        return self.ethereum_rpc_url.get_secret_value()
