"""Unit tests for checkpoint persistence and the indexer Unit of Work."""

import asyncio
import traceback
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ethereum_wallet_intelligence.application import (
    CheckpointConflictError,
    CheckpointRepository,
    CommitOutcomeUnknownError,
    IndexerCheckpoint,
    IndexerUnitOfWork,
    UnitOfWorkCleanupError,
)
from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockHash,
    BlockNumber,
    ChainId,
)
from ethereum_wallet_intelligence.infrastructure.persistence import (
    Base,
    SQLAlchemyBlockRepository,
    SQLAlchemyCheckpointRepository,
    SQLAlchemyIndexerUnitOfWork,
    create_session_factory,
)

HASH_9 = "0x" + "09" * 32
HASH_10 = "0x" + "10" * 32
HASH_11 = "0x" + "11" * 32
WRONG_HASH = "0x" + "ff" * 32


def _checkpoint(
    *,
    chain_id: int = 1,
    start: int = 10,
    next_block: int = 10,
    parent_hash: str = HASH_9,
) -> IndexerCheckpoint:
    return IndexerCheckpoint(
        chain_id=ChainId(chain_id),
        start_block_number=BlockNumber(start),
        next_block_number=BlockNumber(next_block),
        expected_parent_hash=BlockHash(parent_hash),
    )


def _block(number: int, block_hash: str, parent_hash: str) -> Block:
    return Block(
        number=BlockNumber(number),
        hash=BlockHash(block_hash),
        parent_hash=BlockHash(parent_hash),
        timestamp=datetime(2026, 1, 1, 0, 0, number, tzinfo=UTC),
    )


async def _create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


def test_checkpoint_repository_satisfies_application_protocol() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            session_factory = create_session_factory(engine)
            async with session_factory() as session:
                repository: CheckpointRepository = SQLAlchemyCheckpointRepository(session)
                assert await repository.get_for_update() is None
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_checkpoint_create_advance_and_stale_writer_rejection() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            session_factory = create_session_factory(engine)
            initial = _checkpoint()
            block_10 = _block(10, HASH_10, HASH_9)

            async with session_factory() as session, session.begin():
                checkpoints = SQLAlchemyCheckpointRepository(session)
                await checkpoints.create(initial)

            async with session_factory() as session, session.begin():
                await SQLAlchemyBlockRepository(session).save(block_10)

            async with session_factory() as session, session.begin():
                checkpoints = SQLAlchemyCheckpointRepository(session)
                advanced = await checkpoints.advance(initial, block_10)
                assert advanced == _checkpoint(next_block=11, parent_hash=HASH_10)

            async with session_factory() as session, session.begin():
                checkpoints = SQLAlchemyCheckpointRepository(session)
                with pytest.raises(CheckpointConflictError, match="changed concurrently"):
                    await checkpoints.advance(initial, block_10)
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_checkpoint_advance_rejects_skip_wrong_parent_and_chain_mismatch() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            session_factory = create_session_factory(engine)
            initial = _checkpoint()

            async with session_factory() as session, session.begin():
                checkpoints = SQLAlchemyCheckpointRepository(session)
                await checkpoints.create(initial)

            async with session_factory() as session, session.begin():
                checkpoints = SQLAlchemyCheckpointRepository(session)
                with pytest.raises(CheckpointConflictError, match="next block"):
                    await checkpoints.advance(initial, _block(11, HASH_11, HASH_10))

            async with session_factory() as session, session.begin():
                checkpoints = SQLAlchemyCheckpointRepository(session)
                with pytest.raises(CheckpointConflictError, match="parent hash"):
                    await checkpoints.advance(initial, _block(10, HASH_10, WRONG_HASH))

            wrong_chain = _checkpoint(chain_id=8453)
            async with session_factory() as session, session.begin():
                checkpoints = SQLAlchemyCheckpointRepository(session)
                with pytest.raises(CheckpointConflictError, match="changed concurrently"):
                    await checkpoints.advance(wrong_chain, _block(10, HASH_10, HASH_9))
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_checkpoint_rewind_requires_retained_ancestor_and_deleted_descendants() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            session_factory = create_session_factory(engine)
            block_10 = _block(10, HASH_10, HASH_9)
            block_11 = _block(11, HASH_11, HASH_10)
            initial = _checkpoint()

            async with SQLAlchemyIndexerUnitOfWork(session_factory) as unit_of_work:
                await unit_of_work.checkpoints.create(initial)
                await unit_of_work.blocks.save(block_10)
                after_10 = await unit_of_work.checkpoints.advance(initial, block_10)
                await unit_of_work.blocks.save(block_11)
                after_11 = await unit_of_work.checkpoints.advance(after_10, block_11)
                await unit_of_work.commit()

            async with SQLAlchemyIndexerUnitOfWork(session_factory) as unit_of_work:
                with pytest.raises(CheckpointConflictError, match="must be deleted"):
                    await unit_of_work.checkpoints.rewind(after_11, block_10)

            async with SQLAlchemyIndexerUnitOfWork(session_factory) as unit_of_work:
                locked = await unit_of_work.checkpoints.get_for_update()
                assert locked == after_11
                await unit_of_work.blocks.delete_after(block_10.number)
                rewound = await unit_of_work.checkpoints.rewind(after_11, block_10)
                await unit_of_work.commit()

            assert rewound == _checkpoint(next_block=11, parent_hash=HASH_10)
            async with SQLAlchemyIndexerUnitOfWork(session_factory) as unit_of_work:
                assert await unit_of_work.blocks.get_by_number(block_11.number) is None
                assert await unit_of_work.checkpoints.get_for_update() == rewound
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_checkpoint_rewind_rejects_before_start_and_unknown_ancestor() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            session_factory = create_session_factory(engine)
            current = _checkpoint(next_block=12, parent_hash=HASH_11)

            async with session_factory() as session, session.begin():
                await SQLAlchemyCheckpointRepository(session).create(current)

            async with session_factory() as session, session.begin():
                checkpoints = SQLAlchemyCheckpointRepository(session)
                with pytest.raises(CheckpointConflictError, match="start boundary"):
                    await checkpoints.rewind(current, _block(9, HASH_9, WRONG_HASH))

            async with session_factory() as session, session.begin():
                checkpoints = SQLAlchemyCheckpointRepository(session)
                with pytest.raises(CheckpointConflictError, match="not the persisted"):
                    await checkpoints.rewind(current, _block(10, HASH_10, HASH_9))
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_checkpoint_concurrent_initialization_is_rejected() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            session_factory = create_session_factory(engine)
            initial = _checkpoint()

            async with session_factory() as session, session.begin():
                await SQLAlchemyCheckpointRepository(session).create(initial)

            async with session_factory() as session, session.begin():
                with pytest.raises(CheckpointConflictError, match="already exists"):
                    await SQLAlchemyCheckpointRepository(session).create(initial)
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_unit_of_work_explicit_commit_and_exit_without_commit() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            session_factory = create_session_factory(engine)
            unit_of_work: IndexerUnitOfWork = SQLAlchemyIndexerUnitOfWork(session_factory)
            initial = _checkpoint()
            block_10 = _block(10, HASH_10, HASH_9)

            async with unit_of_work:
                await unit_of_work.checkpoints.create(initial)
                await unit_of_work.commit()

            async with SQLAlchemyIndexerUnitOfWork(session_factory) as uncommitted:
                await uncommitted.blocks.save(block_10)

            async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
                assert await reader.checkpoints.get_for_update() == initial
                assert await reader.blocks.get_by_number(block_10.number) is None
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_unit_of_work_cancellation_rolls_back_and_closes() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            session_factory = create_session_factory(engine)
            unit_of_work = SQLAlchemyIndexerUnitOfWork(session_factory)

            with pytest.raises(asyncio.CancelledError):
                async with unit_of_work:
                    await unit_of_work.checkpoints.create(_checkpoint())
                    raise asyncio.CancelledError

            with pytest.raises(RuntimeError, match="not active"):
                _ = unit_of_work.checkpoints

            async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
                assert await reader.checkpoints.get_for_update() is None
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_unit_of_work_classifies_commit_exception_as_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            session_factory = create_session_factory(engine)
            unit_of_work = SQLAlchemyIndexerUnitOfWork(session_factory)

            with pytest.raises(CommitOutcomeUnknownError) as error:
                async with unit_of_work:
                    await unit_of_work.checkpoints.create(_checkpoint())
                    session = unit_of_work._session
                    assert session is not None

                    async def fail_commit() -> None:
                        raise RuntimeError(
                            "postgresql://user:database-secret@database.example/indexer"
                        )

                    monkeypatch.setattr(session, "commit", fail_commit)
                    await unit_of_work.commit()

            rendered = "".join(
                traceback.TracebackException.from_exception(error.value).format(chain=True)
            )
            assert "database-secret" not in rendered
            assert error.value.__cause__ is None
            assert error.value.__context__ is None
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_unit_of_work_rollback_failure_uses_invalidation_and_preserves_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            unit_of_work = SQLAlchemyIndexerUnitOfWork(create_session_factory(engine))
            invalidated = False

            with pytest.raises(CommitOutcomeUnknownError) as error:
                async with unit_of_work:
                    session = unit_of_work._session
                    assert session is not None
                    original_invalidate = session.invalidate

                    async def fail_commit() -> None:
                        raise RuntimeError("database-secret commit failure")

                    async def fail_rollback() -> None:
                        raise RuntimeError("database-secret rollback failure")

                    async def record_invalidate() -> None:
                        nonlocal invalidated
                        invalidated = True
                        await original_invalidate()

                    monkeypatch.setattr(session, "commit", fail_commit)
                    monkeypatch.setattr(session, "rollback", fail_rollback)
                    monkeypatch.setattr(session, "invalidate", record_invalidate)
                    await unit_of_work.commit()

            assert invalidated is True
            rendered = "".join(
                traceback.TracebackException.from_exception(error.value).format(chain=True)
            )
            assert "database-secret" not in rendered
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_unit_of_work_fails_closed_when_rollback_and_invalidation_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            unit_of_work = SQLAlchemyIndexerUnitOfWork(create_session_factory(engine))

            with pytest.raises(UnitOfWorkCleanupError) as error:
                async with unit_of_work:
                    session = unit_of_work._session
                    assert session is not None

                    async def fail_commit() -> None:
                        raise RuntimeError("database-secret commit failure")

                    async def fail_rollback() -> None:
                        raise RuntimeError("database-secret rollback failure")

                    async def fail_invalidate() -> None:
                        raise RuntimeError("database-secret invalidation failure")

                    monkeypatch.setattr(session, "commit", fail_commit)
                    monkeypatch.setattr(session, "rollback", fail_rollback)
                    monkeypatch.setattr(session, "invalidate", fail_invalidate)
                    await unit_of_work.commit()

            rendered = "".join(
                traceback.TracebackException.from_exception(error.value).format(chain=True)
            )
            assert "database-secret" not in rendered
            assert isinstance(error.value.__context__, CommitOutcomeUnknownError)
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_unit_of_work_close_failure_after_ambiguous_commit_preserves_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            unit_of_work = SQLAlchemyIndexerUnitOfWork(create_session_factory(engine))
            invalidated = False

            with pytest.raises(CommitOutcomeUnknownError) as error:
                async with unit_of_work:
                    session = unit_of_work._session
                    assert session is not None
                    original_commit = session.commit
                    original_invalidate = session.invalidate

                    async def commit_then_fail() -> None:
                        await original_commit()
                        raise RuntimeError("database-secret acknowledgment failure")

                    async def fail_close() -> None:
                        raise RuntimeError("database-secret close failure")

                    async def record_invalidate() -> None:
                        nonlocal invalidated
                        invalidated = True
                        await original_invalidate()

                    monkeypatch.setattr(session, "commit", commit_then_fail)
                    monkeypatch.setattr(session, "close", fail_close)
                    monkeypatch.setattr(session, "invalidate", record_invalidate)
                    await unit_of_work.commit()

            assert invalidated is True
            rendered = "".join(
                traceback.TracebackException.from_exception(error.value).format(chain=True)
            )
            assert "database-secret" not in rendered
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_unit_of_work_close_failure_invalidates_and_reports_cleanup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            unit_of_work = SQLAlchemyIndexerUnitOfWork(create_session_factory(engine))
            invalidated = False

            with pytest.raises(UnitOfWorkCleanupError, match="required connection invalidation"):
                async with unit_of_work:
                    session = unit_of_work._session
                    assert session is not None
                    original_invalidate = session.invalidate

                    async def fail_close() -> None:
                        raise RuntimeError("database-secret close failure")

                    async def record_invalidate() -> None:
                        nonlocal invalidated
                        invalidated = True
                        await original_invalidate()

                    monkeypatch.setattr(session, "close", fail_close)
                    monkeypatch.setattr(session, "invalidate", record_invalidate)

            assert invalidated is True
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_unit_of_work_close_and_invalidation_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            unit_of_work = SQLAlchemyIndexerUnitOfWork(create_session_factory(engine))

            with pytest.raises(UnitOfWorkCleanupError, match="could not be confirmed"):
                async with unit_of_work:
                    session = unit_of_work._session
                    assert session is not None

                    async def fail_close() -> None:
                        raise RuntimeError("database-secret close failure")

                    async def fail_invalidate() -> None:
                        raise RuntimeError("database-secret invalidation failure")

                    monkeypatch.setattr(session, "close", fail_close)
                    monkeypatch.setattr(session, "invalidate", fail_invalidate)
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_unit_of_work_cancellation_during_cleanup_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await _create_schema(engine)
            unit_of_work = SQLAlchemyIndexerUnitOfWork(create_session_factory(engine))
            invalidated = False

            with pytest.raises(asyncio.CancelledError):
                async with unit_of_work:
                    session = unit_of_work._session
                    assert session is not None
                    original_invalidate = session.invalidate

                    async def cancel_rollback() -> None:
                        raise asyncio.CancelledError

                    async def record_invalidate() -> None:
                        nonlocal invalidated
                        invalidated = True
                        await original_invalidate()

                    monkeypatch.setattr(session, "rollback", cancel_rollback)
                    monkeypatch.setattr(session, "invalidate", record_invalidate)
                    raise RuntimeError("primary failure")

            assert invalidated is True
        finally:
            await engine.dispose()

    asyncio.run(_run())
