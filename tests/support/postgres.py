"""Fail-closed configuration for the disposable PostgreSQL test database."""

from collections.abc import Callable
from urllib.parse import unquote, urlsplit

from alembic import command
from alembic.config import Config
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

TEST_DATABASE_NAME = "ethereum_wallet_intelligence_test"
_LOCAL_TEST_ENDPOINTS = {
    ("127.0.0.1", 5433),
    ("::1", 5433),
    ("localhost", 5433),
    ("postgres-test", 5432),
}


class UnsafeTestDatabaseError(ValueError):
    """Raised before SQL execution when the test database target is unsafe."""


class PostgreSQLTestSettings(BaseSettings):
    """Test-only database settings, deliberately separate from application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    test_database_url: SecretStr | None = Field(
        default=None,
        validation_alias="TEST_DATABASE_URL",
    )
    application_database_url: SecretStr | None = Field(
        default=None,
        validation_alias="DATABASE_URL",
    )


def _database_target(url: str) -> tuple[str, int, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"postgresql+asyncpg", "postgres+asyncpg"}:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL must use the PostgreSQL asyncpg driver")
    if parsed.hostname is None:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL must include a database host")

    try:
        port = parsed.port or 5432
    except ValueError as error:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL contains an invalid port") from error

    database_name = unquote(parsed.path.removeprefix("/"))
    if not database_name or "/" in database_name:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL must identify one database")

    return parsed.hostname.lower(), port, database_name


def require_safe_test_database_url(settings: PostgreSQLTestSettings) -> str:
    """Return a validated disposable test URL without falling back to DATABASE_URL."""
    if settings.test_database_url is None:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL is required for PostgreSQL integration tests"
        )

    test_url = settings.test_database_url.get_secret_value()
    test_host, test_port, test_database = _database_target(test_url)
    if test_database != TEST_DATABASE_NAME:
        raise UnsafeTestDatabaseError(
            f"TEST_DATABASE_URL must target the dedicated {TEST_DATABASE_NAME!r} database"
        )
    if (test_host, test_port) not in _LOCAL_TEST_ENDPOINTS:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL must target the local disposable PostgreSQL test service"
        )

    if settings.application_database_url is not None:
        application_url = settings.application_database_url.get_secret_value()
        _, _, application_database = _database_target(application_url)
        if application_database == test_database:
            raise UnsafeTestDatabaseError(
                "TEST_DATABASE_URL must not target the application database"
            )

    return test_url


def _upgrade_test_database(database_url: str) -> None:
    alembic_config = Config("alembic.ini")
    alembic_config.attributes["database_url_override"] = database_url
    command.upgrade(alembic_config, "head")


def prepare_test_database(
    settings: PostgreSQLTestSettings | None = None,
    *,
    upgrade: Callable[[str], None] = _upgrade_test_database,
) -> str:
    """Validate safety before the first possible database operation, then migrate."""
    database_url = require_safe_test_database_url(settings or PostgreSQLTestSettings())
    upgrade(database_url)
    return database_url
