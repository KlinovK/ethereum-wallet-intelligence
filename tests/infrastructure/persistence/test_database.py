"""Unit tests for database engine and session factory utilities."""

from ethereum_wallet_intelligence.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)


def test_create_database_engine_and_session_factory() -> None:
    engine = create_database_engine("sqlite+aiosqlite:///:memory:")
    session_factory = create_session_factory(engine)

    assert engine is not None
    assert session_factory is not None
