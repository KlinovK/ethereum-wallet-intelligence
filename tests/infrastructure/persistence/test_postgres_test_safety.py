"""Safety tests for destructive PostgreSQL integration-test setup."""

import pytest
from pydantic import SecretStr

from tests.support.postgres import (
    PostgreSQLTestSettings,
    UnsafeTestDatabaseError,
    prepare_test_database,
    require_safe_test_database_url,
)

SAFE_TEST_URL = (
    "postgresql+asyncpg://test-user:test-password@localhost:5433/ethereum_wallet_intelligence_test"
)
APPLICATION_URL = (
    "postgresql+asyncpg://app-user:app-password@localhost:5432/ethereum_wallet_intelligence"
)


def _settings(
    test_url: str | None,
    application_url: str | None = APPLICATION_URL,
) -> PostgreSQLTestSettings:
    return PostgreSQLTestSettings(
        test_database_url=SecretStr(test_url) if test_url is not None else None,
        application_database_url=(
            SecretStr(application_url) if application_url is not None else None
        ),
    )


def test_missing_test_url_does_not_fall_back_to_application_url() -> None:
    with pytest.raises(UnsafeTestDatabaseError, match="TEST_DATABASE_URL is required"):
        require_safe_test_database_url(_settings(None))


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "postgresql+asyncpg://user:secret@localhost:5432/ethereum_wallet_intelligence",
        "postgresql+asyncpg://user:secret@localhost:5432/ethereum_wallet_intelligence_test",
        "postgresql+asyncpg://user:secret@db.example/ethereum_wallet_intelligence_test",
        "postgresql://user:secret@localhost:5433/ethereum_wallet_intelligence_test",
    ],
)
def test_unsafe_test_database_targets_are_rejected_without_exposing_credentials(
    unsafe_url: str,
) -> None:
    with pytest.raises(UnsafeTestDatabaseError) as error:
        require_safe_test_database_url(_settings(unsafe_url))

    assert "secret" not in str(error.value)


def test_application_database_cannot_be_the_test_database() -> None:
    with pytest.raises(UnsafeTestDatabaseError, match="must not target"):
        require_safe_test_database_url(_settings(SAFE_TEST_URL, SAFE_TEST_URL))


def test_safe_test_database_target_is_accepted() -> None:
    assert require_safe_test_database_url(_settings(SAFE_TEST_URL)) == SAFE_TEST_URL


def test_unsafe_configuration_fails_before_database_upgrade() -> None:
    upgrade_called = False

    def record_upgrade(_: str) -> None:
        nonlocal upgrade_called
        upgrade_called = True

    with pytest.raises(UnsafeTestDatabaseError):
        prepare_test_database(_settings(None), upgrade=record_upgrade)

    assert upgrade_called is False
