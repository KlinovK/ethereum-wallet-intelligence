"""Database engine and session factory configuration."""

from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_database_engine(
    url: str,
    *,
    echo: bool = False,
    pool_pre_ping: bool = True,
    **engine_kwargs: Any,
) -> AsyncEngine:
    """Create an asynchronous SQLAlchemy engine."""
    return create_async_engine(
        url,
        echo=echo,
        pool_pre_ping=pool_pre_ping,
        **engine_kwargs,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to the given engine."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
