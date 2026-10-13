"""Tests for application configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ethereum_wallet_intelligence.config import Settings

_ENVIRONMENT_VARIABLES = (
    "EWI_APP_NAME",
    "EWI_APP_HOST",
    "EWI_APP_PORT",
    "ETHEREUM_RPC_URL",
    "ETHEREUM_RPC_TIMEOUT_SECONDS",
    "ETHEREUM_RPC_MAX_ATTEMPTS",
    "ETHEREUM_RPC_RETRY_BASE_DELAY_SECONDS",
    "ETHEREUM_RPC_RETRY_MAX_DELAY_SECONDS",
    "ETHEREUM_RPC_RETRY_JITTER_RATIO",
    "DATABASE_URL",
)


@pytest.fixture(autouse=True)
def isolate_settings_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for variable_name in _ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable_name, raising=False)
    monkeypatch.chdir(tmp_path)


def test_settings_use_defaults() -> None:
    settings = Settings()

    assert settings.app_name == "Ethereum Wallet Intelligence"
    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 8000
    assert settings.ethereum_rpc_url is None
    assert settings.ethereum_rpc_timeout_seconds == 10
    assert settings.ethereum_rpc_max_attempts == 3
    assert settings.ethereum_rpc_retry_base_delay_seconds == 0.25
    assert settings.ethereum_rpc_retry_max_delay_seconds == 2
    assert settings.ethereum_rpc_retry_jitter_ratio == 0.2
    assert settings.database_url is None


def test_settings_read_supported_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EWI_APP_NAME", "Wallet API")
    monkeypatch.setenv("EWI_APP_HOST", "0.0.0.0")
    monkeypatch.setenv("EWI_APP_PORT", "9000")
    monkeypatch.setenv("ETHEREUM_RPC_URL", "https://ethereum.example/rpc/project-token")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://user:pass@localhost:5432/ethereum_wallet_intelligence",
    )

    settings = Settings()

    assert settings.app_name == "Wallet API"
    assert settings.app_host == "0.0.0.0"
    assert settings.app_port == 9000
    assert settings.require_ethereum_rpc_url() == "https://ethereum.example/rpc/project-token"
    assert (
        settings.require_database_url()
        == "postgresql+asyncpg://user:pass@localhost:5432/ethereum_wallet_intelligence"
    )


def test_settings_reject_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EWI_APP_PORT", "70000")

    with pytest.raises(ValidationError, match="less than or equal to 65535"):
        Settings()


def test_rpc_url_is_optional_until_rpc_access_is_requested() -> None:
    settings = Settings()

    with pytest.raises(ValueError, match="ETHEREUM_RPC_URL is required"):
        settings.require_ethereum_rpc_url()


def test_database_url_is_optional_until_database_access_is_requested() -> None:
    settings = Settings()

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        settings.require_database_url()


def test_settings_reject_invalid_rpc_url_without_exposing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rpc_url = "ftp://user:super-secret@ethereum.example/rpc"
    monkeypatch.setenv("ETHEREUM_RPC_URL", rpc_url)

    with pytest.raises(ValidationError) as error:
        Settings()

    assert "valid HTTP(S) URL" in str(error.value)
    assert "super-secret" not in str(error.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ETHEREUM_RPC_TIMEOUT_SECONDS", "0"),
        ("ETHEREUM_RPC_TIMEOUT_SECONDS", "61"),
        ("ETHEREUM_RPC_MAX_ATTEMPTS", "0"),
        ("ETHEREUM_RPC_MAX_ATTEMPTS", "6"),
        ("ETHEREUM_RPC_RETRY_BASE_DELAY_SECONDS", "6"),
        ("ETHEREUM_RPC_RETRY_MAX_DELAY_SECONDS", "31"),
        ("ETHEREUM_RPC_RETRY_JITTER_RATIO", "1.1"),
    ],
)
def test_settings_reject_unbounded_rpc_reliability_values(
    name: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()


def test_settings_reject_retry_maximum_below_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ETHEREUM_RPC_RETRY_BASE_DELAY_SECONDS", "2")
    monkeypatch.setenv("ETHEREUM_RPC_RETRY_MAX_DELAY_SECONDS", "1")

    with pytest.raises(ValidationError, match="cannot be below"):
        Settings()


@pytest.mark.parametrize(
    "invalid_url",
    [
        "sqlite+aiosqlite:///file.db",
        "https://database.example",
        "postgresql://user:secret@localhost:5432/db",
        "invalid-url",
    ],
)
def test_settings_reject_invalid_database_url_without_exposing_credentials(
    invalid_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", invalid_url)

    with pytest.raises(ValidationError) as error:
        Settings()

    assert "PostgreSQL asyncpg URL" in str(error.value)
    assert "secret" not in str(error.value)


def test_settings_load_repository_style_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dotenv_rpc_url = "https://dotenv.ethereum.example/rpc/token"
    dotenv_db_url = "postgresql+asyncpg://user:pass@localhost:5432/dotenv_db"
    (tmp_path / ".env").write_text(
        f"ETHEREUM_RPC_URL={dotenv_rpc_url}\nDATABASE_URL={dotenv_db_url}\n",
        encoding="utf-8",
    )
    settings = Settings()

    assert settings.require_ethereum_rpc_url() == dotenv_rpc_url
    assert settings.require_database_url() == dotenv_db_url


def test_environment_variable_overrides_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dotenv_rpc_url = "https://dotenv.ethereum.example/rpc/token"
    environment_rpc_url = "https://environment.ethereum.example/rpc/token"
    dotenv_db_url = "postgresql+asyncpg://user:pass@localhost:5432/dotenv_db"
    environment_db_url = "postgresql+asyncpg://user:pass@localhost:5432/env_db"
    (tmp_path / ".env").write_text(
        f"ETHEREUM_RPC_URL={dotenv_rpc_url}\nDATABASE_URL={dotenv_db_url}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ETHEREUM_RPC_URL", environment_rpc_url)
    monkeypatch.setenv("DATABASE_URL", environment_db_url)

    settings = Settings()

    assert settings.require_ethereum_rpc_url() == environment_rpc_url
    assert settings.require_database_url() == environment_db_url
