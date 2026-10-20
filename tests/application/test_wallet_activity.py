"""Tests for framework-independent wallet activity application contracts."""

import asyncio
import base64
import json
import traceback
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from ethereum_wallet_intelligence.application import (
    InvalidWalletActivityCursorError,
    InvalidWalletActivityQueryError,
    ListWalletActivity,
    StaleWalletActivityCursorError,
    WalletActivityCoverage,
    WalletActivityCursor,
    WalletActivityDirection,
    WalletActivityIndexUnavailableError,
    WalletActivityPage,
    WalletActivityQuery,
    WalletActivityReadError,
    WalletTransactionActivity,
    classify_wallet_activity_direction,
    decode_wallet_activity_cursor,
    encode_wallet_activity_cursor,
)
from ethereum_wallet_intelligence.domain.ethereum import (
    BlockHash,
    BlockNumber,
    ChainId,
    EthereumAddress,
    TransactionHash,
    Wei,
)

WALLET = EthereumAddress("0x" + "11" * 20)
OTHER = EthereumAddress("0x" + "22" * 20)
THIRD = EthereumAddress("0x" + "33" * 20)
BLOCK_HASH = BlockHash("0x" + "aa" * 32)
TIMESTAMP = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)


def _transaction_hash(byte: str = "bb") -> TransactionHash:
    return TransactionHash("0x" + byte * 32)


def _activity(
    *,
    transaction_hash: TransactionHash | None = None,
    block_number: int = 12,
    direction: WalletActivityDirection = WalletActivityDirection.INCOMING,
    sender: EthereumAddress = OTHER,
    recipient: EthereumAddress | None = WALLET,
    value: int = 1,
    nonce: int = 0,
    gas_limit: int = 21_000,
    has_input: bool = False,
    timestamp: datetime = TIMESTAMP,
) -> WalletTransactionActivity:
    return WalletTransactionActivity(
        transaction_hash=transaction_hash or _transaction_hash(),
        block_number=BlockNumber(block_number),
        block_hash=BLOCK_HASH,
        block_timestamp=timestamp,
        direction=direction,
        sender=sender,
        recipient=recipient,
        value=Wei(value),
        nonce=nonce,
        gas_limit=gas_limit,
        has_input=has_input,
    )


def _coverage(
    *,
    start: int = 10,
    through: int | None = 20,
) -> WalletActivityCoverage:
    return WalletActivityCoverage(
        chain_id=ChainId.ethereum_mainnet(),
        indexed_from_block=BlockNumber(start),
        indexed_through_block=BlockNumber(through) if through is not None else None,
        indexed_through_hash=BLOCK_HASH if through is not None else None,
    )


def _cursor(
    *,
    wallet: EthereumAddress = WALLET,
    direction: WalletActivityDirection | None = None,
    snapshot: int = 20,
    last_block: int = 12,
    last_hash: TransactionHash | None = None,
) -> WalletActivityCursor:
    return WalletActivityCursor(
        version=1,
        wallet_address=wallet,
        direction=direction,
        snapshot_block_number=BlockNumber(snapshot),
        snapshot_block_hash=BLOCK_HASH,
        last_block_number=BlockNumber(last_block),
        last_transaction_hash=last_hash or _transaction_hash(),
    )


def _page(
    *,
    activities: tuple[WalletTransactionActivity, ...] | None = None,
    direction: WalletActivityDirection | None = None,
    next_cursor: WalletActivityCursor | None = None,
    coverage: WalletActivityCoverage | None = None,
) -> WalletActivityPage:
    return WalletActivityPage(
        wallet_address=WALLET,
        direction=direction,
        coverage=coverage or _coverage(),
        activities=activities if activities is not None else (_activity(),),
        next_cursor=next_cursor,
    )


def _encode_payload(payload: object) -> str:
    serialized = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(serialized).rstrip(b"=").decode()


def _valid_payload() -> dict[str, object]:
    return {
        "v": 1,
        "a": WALLET.value,
        "d": "all",
        "sb": 20,
        "sh": BLOCK_HASH.value,
        "lb": 12,
        "lh": _transaction_hash().value,
    }


@pytest.mark.parametrize(
    ("sender", "recipient", "expected"),
    [
        (OTHER, WALLET, WalletActivityDirection.INCOMING),
        (WALLET, OTHER, WalletActivityDirection.OUTGOING),
        (WALLET, WALLET, WalletActivityDirection.SELF),
        (WALLET, None, WalletActivityDirection.OUTGOING),
    ],
)
def test_classifies_wallet_activity_direction(
    sender: EthereumAddress,
    recipient: EthereumAddress | None,
    expected: WalletActivityDirection,
) -> None:
    assert classify_wallet_activity_direction(WALLET, sender, recipient) is expected


def test_direction_classification_rejects_unrelated_transaction() -> None:
    with pytest.raises(WalletActivityReadError, match="does not involve"):
        classify_wallet_activity_direction(WALLET, OTHER, THIRD)


def test_activity_supports_self_contract_creation_and_zero_value() -> None:
    self_activity = _activity(
        direction=WalletActivityDirection.SELF,
        sender=WALLET,
        recipient=WALLET,
        value=0,
    )
    contract_creation = _activity(
        direction=WalletActivityDirection.OUTGOING,
        sender=WALLET,
        recipient=None,
        has_input=True,
    )

    assert self_activity.value == Wei(0)
    assert not self_activity.is_contract_creation
    assert contract_creation.is_contract_creation
    assert contract_creation.has_input


def test_activity_normalizes_timestamp_to_utc_and_is_immutable() -> None:
    offset_timestamp = datetime(2025, 1, 2, 6, 4, 5, tzinfo=timezone(timedelta(hours=3)))
    activity = _activity(timestamp=offset_timestamp)

    assert activity.block_timestamp == TIMESTAMP
    assert activity.block_timestamp.tzinfo is UTC
    with pytest.raises(FrozenInstanceError):
        activity.nonce = 1  # type: ignore[misc]


def test_query_cursor_coverage_and_page_are_immutable() -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    cursor = _cursor()
    coverage = _coverage()
    page = _page()

    with pytest.raises(FrozenInstanceError):
        query.limit = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        cursor.version = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        coverage.indexed_through_block = None  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        page.activities = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"), [("nonce", -1), ("nonce", True), ("gas_limit", -1), ("gas_limit", False)]
)
def test_activity_rejects_invalid_integer_fields(field: str, value: int) -> None:
    arguments: dict[str, object] = {field: value}
    with pytest.raises(WalletActivityReadError):
        _activity(**arguments)  # type: ignore[arg-type]


def test_activity_rejects_naive_or_pre_epoch_timestamp() -> None:
    with pytest.raises(WalletActivityReadError, match="timezone"):
        _activity(timestamp=datetime(2025, 1, 1))
    with pytest.raises(WalletActivityReadError, match="Unix epoch"):
        _activity(timestamp=datetime(1969, 12, 31, tzinfo=UTC))


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime.max.replace(tzinfo=timezone(-timedelta(hours=1))),
        datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_activity_safely_rejects_utc_normalization_overflow(timestamp: datetime) -> None:
    with pytest.raises(WalletActivityReadError, match="timestamp is invalid") as raised:
        _activity(timestamp=timestamp)

    rendered = "".join(
        traceback.format_exception(type(raised.value), raised.value, raised.value.__traceback__)
    )
    assert raised.value.__cause__ is None
    assert "OverflowError" not in rendered
    assert "date value out of range" not in rendered


def test_activity_rejects_non_boolean_input_marker() -> None:
    with pytest.raises(WalletActivityReadError, match="must be a boolean"):
        _activity(has_input=cast(bool, 1))


def test_coverage_accepts_empty_and_non_empty_mainnet_ranges() -> None:
    assert _coverage().indexed_through_block == BlockNumber(20)
    assert _coverage(through=None).indexed_through_block is None


def test_coverage_rejects_non_mainnet_chain() -> None:
    with pytest.raises(WalletActivityReadError, match="Mainnet"):
        WalletActivityCoverage(
            chain_id=ChainId(8453),
            indexed_from_block=BlockNumber(10),
            indexed_through_block=None,
            indexed_through_hash=None,
        )


def test_coverage_rejects_incomplete_tip_identity() -> None:
    with pytest.raises(WalletActivityReadError, match="both be present or absent"):
        WalletActivityCoverage(
            chain_id=ChainId.ethereum_mainnet(),
            indexed_from_block=BlockNumber(10),
            indexed_through_block=BlockNumber(20),
            indexed_through_hash=None,
        )


def test_coverage_rejects_end_before_start() -> None:
    with pytest.raises(WalletActivityReadError, match="cannot precede"):
        _coverage(start=10, through=9)


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_query_rejects_invalid_limits(limit: object) -> None:
    with pytest.raises(InvalidWalletActivityQueryError, match="limit"):
        WalletActivityQuery(wallet_address=WALLET, limit=cast(int, limit))


def test_query_defaults_to_fifty_and_accepts_limit_boundaries() -> None:
    assert WalletActivityQuery(wallet_address=WALLET).limit == 50
    assert WalletActivityQuery(wallet_address=WALLET, limit=1).limit == 1
    assert WalletActivityQuery(wallet_address=WALLET, limit=100).limit == 100


def test_query_rejects_cursor_address_or_direction_mismatch() -> None:
    with pytest.raises(InvalidWalletActivityQueryError, match="does not match"):
        WalletActivityQuery(wallet_address=OTHER, cursor=_cursor())
    with pytest.raises(InvalidWalletActivityQueryError, match="does not match"):
        WalletActivityQuery(
            wallet_address=WALLET,
            direction=WalletActivityDirection.INCOMING,
            cursor=_cursor(),
        )


def test_page_accepts_strict_descending_activity_and_matching_cursor() -> None:
    first = _activity(transaction_hash=_transaction_hash("cc"), block_number=13)
    last = _activity(transaction_hash=_transaction_hash("bb"), block_number=12)
    cursor = _cursor(last_block=12, last_hash=last.transaction_hash)

    page = _page(activities=(first, last), next_cursor=cursor)

    assert page.activities == (first, last)
    assert page.next_cursor == cursor


def test_page_accepts_descending_transaction_hashes_within_one_block() -> None:
    first = _activity(transaction_hash=_transaction_hash("cc"), block_number=12)
    second = _activity(transaction_hash=_transaction_hash("bb"), block_number=12)

    assert _page(activities=(first, second)).activities == (first, second)


def test_page_rejects_duplicate_transaction_hashes() -> None:
    duplicate = _transaction_hash()
    with pytest.raises(WalletActivityReadError, match="duplicate"):
        _page(
            activities=(
                _activity(transaction_hash=duplicate, block_number=13),
                _activity(transaction_hash=duplicate, block_number=12),
            )
        )


def test_page_rejects_incorrect_ordering() -> None:
    with pytest.raises(WalletActivityReadError, match="descending"):
        _page(
            activities=(
                _activity(transaction_hash=_transaction_hash("aa"), block_number=12),
                _activity(transaction_hash=_transaction_hash("bb"), block_number=13),
            )
        )


def test_page_rejects_incorrect_same_block_hash_ordering() -> None:
    with pytest.raises(WalletActivityReadError, match="descending"):
        _page(
            activities=(
                _activity(transaction_hash=_transaction_hash("aa")),
                _activity(transaction_hash=_transaction_hash("bb")),
            )
        )


def test_page_rejects_wrong_wallet_involvement() -> None:
    with pytest.raises(WalletActivityReadError, match="does not involve"):
        _page(activities=(_activity(sender=OTHER, recipient=THIRD),))


def test_page_rejects_incorrect_direction_or_filter() -> None:
    with pytest.raises(WalletActivityReadError, match="direction is inconsistent"):
        _page(
            activities=(
                _activity(
                    direction=WalletActivityDirection.OUTGOING,
                    sender=OTHER,
                    recipient=WALLET,
                ),
            )
        )
    with pytest.raises(WalletActivityReadError, match="page filter"):
        _page(
            direction=WalletActivityDirection.OUTGOING,
            activities=(_activity(),),
        )


@pytest.mark.parametrize("block_number", [9, 21])
def test_page_rejects_activity_outside_coverage(block_number: int) -> None:
    with pytest.raises(WalletActivityReadError, match="outside indexed coverage"):
        _page(activities=(_activity(block_number=block_number),))


def test_page_rejects_activity_when_coverage_has_no_tip() -> None:
    with pytest.raises(WalletActivityReadError, match="outside indexed coverage"):
        _page(coverage=_coverage(through=None))


def test_page_rejects_tip_activity_with_conflicting_block_hash() -> None:
    conflicting = replace(
        _activity(block_number=20),
        block_hash=BlockHash("0x" + "dd" * 32),
    )
    with pytest.raises(WalletActivityReadError, match="block identity"):
        _page(activities=(conflicting,))


def test_page_rejects_invalid_next_cursor() -> None:
    activity = _activity()
    with pytest.raises(WalletActivityReadError, match="does not match"):
        _page(
            activities=(activity,),
            next_cursor=_cursor(last_block=11),
        )


def test_empty_page_cannot_have_cursor_but_supports_empty_coverage() -> None:
    page = _page(activities=(), coverage=_coverage(through=None))
    assert page.activities == ()

    with pytest.raises(WalletActivityReadError, match="empty"):
        _page(activities=(), next_cursor=_cursor())


def test_cursor_round_trip_preserves_normalized_values() -> None:
    cursor = _cursor(direction=WalletActivityDirection.SELF)

    assert decode_wallet_activity_cursor(encode_wallet_activity_cursor(cursor)) == cursor


def test_cursor_encoding_is_deterministic_canonical_and_unpadded() -> None:
    cursor = _cursor()
    expected_json = json.dumps(
        _valid_payload(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    expected = base64.urlsafe_b64encode(expected_json).rstrip(b"=").decode("ascii")

    assert encode_wallet_activity_cursor(cursor) == expected
    assert encode_wallet_activity_cursor(cursor) == encode_wallet_activity_cursor(cursor)
    assert "=" not in expected


def test_cursor_decoder_rejects_oversized_input() -> None:
    with pytest.raises(InvalidWalletActivityCursorError, match="maximum encoded length"):
        decode_wallet_activity_cursor("A" * 513)


@pytest.mark.parametrize("encoded", ["", "***", "abcd=", "A"])
def test_cursor_decoder_rejects_invalid_base64_or_padding(encoded: str) -> None:
    with pytest.raises(InvalidWalletActivityCursorError, match="invalid"):
        decode_wallet_activity_cursor(encoded)


def test_cursor_decoder_rejects_invalid_utf8() -> None:
    encoded = base64.urlsafe_b64encode(b"\xff\xfe").rstrip(b"=").decode()
    with pytest.raises(InvalidWalletActivityCursorError, match="invalid"):
        decode_wallet_activity_cursor(encoded)


def test_cursor_decoder_rejects_non_ascii_input() -> None:
    with pytest.raises(InvalidWalletActivityCursorError, match="invalid"):
        decode_wallet_activity_cursor("é")


def test_cursor_decoder_rejects_malformed_json() -> None:
    encoded = base64.urlsafe_b64encode(b"{").rstrip(b"=").decode()
    with pytest.raises(InvalidWalletActivityCursorError, match="invalid"):
        decode_wallet_activity_cursor(encoded)


def test_cursor_decoder_rejects_json_trailing_content() -> None:
    payload = json.dumps(_valid_payload(), separators=(",", ":")).encode() + b"{}"
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    with pytest.raises(InvalidWalletActivityCursorError, match="invalid"):
        decode_wallet_activity_cursor(encoded)


def test_cursor_decoder_rejects_non_object_json() -> None:
    with pytest.raises(InvalidWalletActivityCursorError, match="invalid"):
        decode_wallet_activity_cursor(_encode_payload([1, 2, 3]))


def test_cursor_decoder_rejects_duplicate_json_keys() -> None:
    duplicate_json = (
        '{"v":1,"v":1,"a":"'
        + WALLET.value
        + '","d":"all","sb":20,"sh":"'
        + BLOCK_HASH.value
        + '","lb":12,"lh":"'
        + _transaction_hash().value
        + '"}'
    ).encode()
    encoded = base64.urlsafe_b64encode(duplicate_json).rstrip(b"=").decode()

    with pytest.raises(InvalidWalletActivityCursorError, match="invalid"):
        decode_wallet_activity_cursor(encoded)


@pytest.mark.parametrize("change", ["missing", "unknown"])
def test_cursor_decoder_rejects_missing_or_unknown_keys(change: str) -> None:
    payload = _valid_payload()
    if change == "missing":
        del payload["lh"]
    else:
        payload["extra"] = "value"

    with pytest.raises(InvalidWalletActivityCursorError, match="fields"):
        decode_wallet_activity_cursor(_encode_payload(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("v", True),
        ("a", 1),
        ("d", False),
        ("sb", True),
        ("sh", 1),
        ("lb", False),
        ("lh", 1),
    ],
)
def test_cursor_decoder_rejects_wrong_primitive_types(field: str, value: object) -> None:
    payload = _valid_payload()
    payload[field] = value

    with pytest.raises(InvalidWalletActivityCursorError, match="field types"):
        decode_wallet_activity_cursor(_encode_payload(payload))


def test_cursor_decoder_rejects_unsupported_version() -> None:
    payload = _valid_payload()
    payload["v"] = 2
    with pytest.raises(InvalidWalletActivityCursorError, match="unsupported"):
        decode_wallet_activity_cursor(_encode_payload(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [("a", "0x1234"), ("sh", "0x1234"), ("lh", "0x1234"), ("d", "sideways")],
)
def test_cursor_decoder_rejects_malformed_values(field: str, value: object) -> None:
    payload = _valid_payload()
    payload[field] = value
    with pytest.raises(InvalidWalletActivityCursorError, match="invalid"):
        decode_wallet_activity_cursor(_encode_payload(payload))


def test_cursor_rejects_last_block_after_snapshot() -> None:
    with pytest.raises(InvalidWalletActivityCursorError, match="cannot exceed"):
        _cursor(snapshot=20, last_block=21)

    payload = _valid_payload()
    payload["lb"] = 21
    with pytest.raises(InvalidWalletActivityCursorError, match="invalid"):
        decode_wallet_activity_cursor(_encode_payload(payload))


def test_cursor_encoder_safely_rejects_extreme_block_number() -> None:
    cursor = _cursor(snapshot=10**5000)

    with pytest.raises(InvalidWalletActivityCursorError, match="maximum encoded length") as raised:
        encode_wallet_activity_cursor(cursor)

    assert raised.value.__cause__ is None
    assert "digits" not in str(raised.value)


def test_cursor_encoder_sanitizes_serialization_boundary_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_detail = "serialization-sensitive-detail"

    def fail_serialization(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise ValueError(sensitive_detail)

    monkeypatch.setattr(json, "dumps", fail_serialization)

    with pytest.raises(InvalidWalletActivityCursorError, match="cursor is invalid") as raised:
        encode_wallet_activity_cursor(_cursor())

    rendered = "".join(
        traceback.format_exception(type(raised.value), raised.value, raised.value.__traceback__)
    )
    assert raised.value.__cause__ is None
    assert sensitive_detail not in rendered


class _RecordingReader:
    def __init__(self, page: WalletActivityPage) -> None:
        self.page = page
        self.queries: list[WalletActivityQuery] = []

    async def read_page(self, query: WalletActivityQuery) -> WalletActivityPage:
        self.queries.append(query)
        return self.page


class _FailingReader:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def read_page(self, query: WalletActivityQuery) -> WalletActivityPage:
        del query
        raise self.error


class _NonPageReader:
    async def read_page(self, query: WalletActivityQuery) -> WalletActivityPage:
        del query
        return cast(WalletActivityPage, object())


def test_use_case_delegates_and_returns_validated_page() -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    page = _page()
    reader = _RecordingReader(page)

    result = asyncio.run(ListWalletActivity(reader).execute(query))

    assert result is page
    assert reader.queries == [query]


def test_use_case_rejects_malformed_reader_output() -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    page = _page()
    object.__setattr__(page, "activities", cast(tuple[WalletTransactionActivity, ...], []))
    reader = _RecordingReader(page)

    with pytest.raises(WalletActivityReadError, match="must be a tuple"):
        asyncio.run(ListWalletActivity(reader).execute(query))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("nonce", -1),
        ("gas_limit", -1),
        ("direction", cast(WalletActivityDirection, "incoming")),
        ("block_timestamp", datetime(2025, 1, 1)),
    ],
)
def test_use_case_rejects_mutated_nested_activity(field: str, value: object) -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    activity = _activity()
    page = _page(activities=(activity,))
    object.__setattr__(activity, field, value)

    with pytest.raises(WalletActivityReadError):
        asyncio.run(ListWalletActivity(_RecordingReader(page)).execute(query))


def test_use_case_rejects_mutated_nested_value_object() -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    activity = _activity()
    page = _page(activities=(activity,))
    object.__setattr__(activity.block_number, "value", -1)

    with pytest.raises(WalletActivityReadError, match="block number is invalid"):
        asyncio.run(ListWalletActivity(_RecordingReader(page)).execute(query))


def test_use_case_rejects_mutated_coverage_chain() -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    coverage = _coverage()
    page = _page(coverage=coverage)
    object.__setattr__(coverage, "chain_id", ChainId(8453))

    with pytest.raises(WalletActivityReadError, match="Mainnet"):
        asyncio.run(ListWalletActivity(_RecordingReader(page)).execute(query))


def test_use_case_rejects_mutated_coverage_block_hash_pairing() -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    coverage = _coverage()
    page = _page(coverage=coverage)
    object.__setattr__(coverage, "indexed_through_hash", None)

    with pytest.raises(WalletActivityReadError, match="both be present or absent"):
        asyncio.run(ListWalletActivity(_RecordingReader(page)).execute(query))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 2),
        ("wallet_address", OTHER),
        ("last_block_number", BlockNumber(21)),
    ],
)
def test_use_case_rejects_mutated_next_cursor(field: str, value: object) -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    activity = _activity()
    cursor = _cursor(last_hash=activity.transaction_hash)
    page = _page(activities=(activity,), next_cursor=cursor)
    object.__setattr__(cursor, field, value)

    with pytest.raises(WalletActivityReadError):
        asyncio.run(ListWalletActivity(_RecordingReader(page)).execute(query))


def test_use_case_rejects_mutated_query_limit_before_delegation() -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    object.__setattr__(query, "limit", 0)
    reader = _RecordingReader(_page())

    with pytest.raises(InvalidWalletActivityQueryError, match="limit"):
        asyncio.run(ListWalletActivity(reader).execute(query))

    assert reader.queries == []


def test_use_case_rejects_mutated_query_cursor_mismatch_before_delegation() -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    object.__setattr__(query, "cursor", _cursor(wallet=OTHER))
    reader = _RecordingReader(_page())

    with pytest.raises(InvalidWalletActivityQueryError, match="does not match"):
        asyncio.run(ListWalletActivity(reader).execute(query))

    assert reader.queries == []


def test_use_case_rejects_non_page_reader_output() -> None:
    query = WalletActivityQuery(wallet_address=WALLET)

    with pytest.raises(WalletActivityReadError, match="invalid page"):
        asyncio.run(ListWalletActivity(_NonPageReader()).execute(query))


def test_use_case_rejects_mismatched_or_oversized_page() -> None:
    query = WalletActivityQuery(wallet_address=WALLET, limit=1)
    mismatched = WalletActivityPage(
        wallet_address=OTHER,
        direction=None,
        coverage=_coverage(),
        activities=(_activity(sender=WALLET, recipient=OTHER),),
        next_cursor=None,
    )
    with pytest.raises(WalletActivityReadError, match="mismatched"):
        asyncio.run(ListWalletActivity(_RecordingReader(mismatched)).execute(query))

    first = _activity(transaction_hash=_transaction_hash("cc"), block_number=13)
    second = _activity(transaction_hash=_transaction_hash("bb"), block_number=12)
    oversized = _page(activities=(first, second))
    with pytest.raises(WalletActivityReadError, match="exceeded"):
        asyncio.run(ListWalletActivity(_RecordingReader(oversized)).execute(query))


def test_use_case_preserves_typed_application_error() -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    error = WalletActivityIndexUnavailableError("Wallet activity index is unavailable")

    with pytest.raises(WalletActivityIndexUnavailableError) as raised:
        asyncio.run(ListWalletActivity(_FailingReader(error)).execute(query))

    assert raised.value is error
    assert issubclass(StaleWalletActivityCursorError, WalletActivityReadError)


def test_use_case_preserves_stale_cursor_error() -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    error = StaleWalletActivityCursorError("Wallet activity cursor is stale")

    with pytest.raises(StaleWalletActivityCursorError) as raised:
        asyncio.run(ListWalletActivity(_FailingReader(error)).execute(query))

    assert raised.value is error


def test_use_case_does_not_reclassify_reader_programming_error() -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    error = RuntimeError("reader programming defect")

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(ListWalletActivity(_FailingReader(error)).execute(query))

    assert raised.value is error
    assert not isinstance(raised.value, WalletActivityIndexUnavailableError)


def test_use_case_preserves_cancellation() -> None:
    query = WalletActivityQuery(wallet_address=WALLET)
    cancellation = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError) as raised:
        asyncio.run(ListWalletActivity(_FailingReader(cancellation)).execute(query))

    assert raised.value is cancellation
