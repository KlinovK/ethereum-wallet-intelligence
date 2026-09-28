"""Application configuration."""

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

_ENVIRONMENT_FIELDS = {
    "EWI_APP_NAME": "app_name",
    "EWI_APP_HOST": "app_host",
    "EWI_APP_PORT": "app_port",
}


class Settings(BaseModel):
    """Validated settings read from the process environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    app_name: str = Field(default="Ethereum Wallet Intelligence", min_length=1)
    app_host: str = Field(default="127.0.0.1", min_length=1)
    app_port: int = Field(default=8000, ge=1, le=65535)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "Settings":
        """Build settings from supported environment variables."""
        source = os.environ if environment is None else environment
        values = {
            field_name: source[environment_name]
            for environment_name, field_name in _ENVIRONMENT_FIELDS.items()
            if environment_name in source
        }
        return cls.model_validate(values)
