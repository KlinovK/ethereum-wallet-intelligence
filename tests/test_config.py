"""Tests for application configuration."""

import pytest
from pydantic import ValidationError

from ethereum_wallet_intelligence.config import Settings


def test_settings_use_defaults() -> None:
    settings = Settings.from_environment({})

    assert settings.app_name == "Ethereum Wallet Intelligence"
    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 8000


def test_settings_read_supported_environment_variables() -> None:
    settings = Settings.from_environment(
        {
            "EWI_APP_NAME": "Wallet API",
            "EWI_APP_HOST": "0.0.0.0",
            "EWI_APP_PORT": "9000",
        }
    )

    assert settings.app_name == "Wallet API"
    assert settings.app_host == "0.0.0.0"
    assert settings.app_port == 9000


def test_settings_reject_invalid_port() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 65535"):
        Settings.from_environment({"EWI_APP_PORT": "70000"})
