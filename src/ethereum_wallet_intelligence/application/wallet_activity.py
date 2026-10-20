"""Framework-independent contracts for reading indexed wallet activity."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast

from ethereum_wallet_intelligence.domain.ethereum import (
    BlockHash,
    BlockNumber,
    ChainId,
    DomainValidationError,
    EthereumAddress,
    TransactionHash,
    Wei,
)

_CURSOR_VERSION = 1
_MAX_CURSOR_LENGTH = 512
_CURSOR_KEYS = frozenset({"v", "a", "d", "sb", "sh", "lb", "lh"})
_CURSOR_ALPHABET = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_CURSOR_JSON_BYTES = _MAX_CURSOR_LENGTH * 3 // 4
_MAX_CURSOR_INTEGER = 10**_MAX_CURSOR_JSON_BYTES
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class InvalidWalletActivityQueryError(ValueError):
    """Raised when a wallet activity query violates its public contract."""


class InvalidWalletActivityCursorError(ValueError):
    """Raised when a wallet activity cursor cannot be decoded or validated."""


class WalletActivityReadError(RuntimeError):
    """Raised when wallet activity cannot be returned safely."""


class StaleWalletActivityCursorError(WalletActivityReadError):
    """Raised when a valid cursor no longer identifies the indexed snapshot."""


class WalletActivityIndexUnavailableError(WalletActivityReadError):
    """Raised when indexed wallet activity is temporarily unavailable."""


class WalletActivityDirection(StrEnum):
    """How an indexed transaction relates to the requested wallet."""

    INCOMING = "incoming"
    OUTGOING = "outgoing"
    SELF = "self"


type _ErrorType = type[ValueError | RuntimeError]
type _StringValueObjectType = type[EthereumAddress] | type[BlockHash] | type[TransactionHash]
type _IntegerValueObjectType = type[BlockNumber] | type[ChainId] | type[Wei]


def _validate_string_value_object(
    value: object,
    expected_type: _StringValueObjectType,
    name: str,
    error_type: _ErrorType,
) -> None:
    if not isinstance(value, expected_type):
        raise error_type(f"{name} has an invalid application type")
    try:
        canonical = expected_type(value.value)
    except (AttributeError, DomainValidationError):
        raise error_type(f"{name} is invalid") from None
    if canonical != value:
        raise error_type(f"{name} is not canonical")


def _validate_integer_value_object(
    value: object,
    expected_type: _IntegerValueObjectType,
    name: str,
    error_type: _ErrorType,
) -> None:
    if not isinstance(value, expected_type):
        raise error_type(f"{name} has an invalid application type")
    try:
        canonical = expected_type(value.value)
    except (AttributeError, DomainValidationError):
        raise error_type(f"{name} is invalid") from None
    if canonical != value:
        raise error_type(f"{name} is not canonical")


def _validate_optional_direction(
    direction: WalletActivityDirection | None,
    *,
    error_type: _ErrorType,
) -> None:
    if direction is not None and not isinstance(direction, WalletActivityDirection):
        raise error_type("Wallet activity direction is invalid")


def classify_wallet_activity_direction(
    wallet_address: EthereumAddress,
    sender: EthereumAddress,
    recipient: EthereumAddress | None,
) -> WalletActivityDirection:
    """Classify one transaction relative to a wallet, rejecting unrelated data."""
    _validate_string_value_object(
        wallet_address,
        EthereumAddress,
        "Wallet address",
        WalletActivityReadError,
    )
    _validate_string_value_object(
        sender,
        EthereumAddress,
        "Transaction sender",
        WalletActivityReadError,
    )
    if recipient is not None:
        _validate_string_value_object(
            recipient,
            EthereumAddress,
            "Transaction recipient",
            WalletActivityReadError,
        )

    if sender == wallet_address and recipient == wallet_address:
        return WalletActivityDirection.SELF
    if sender == wallet_address:
        return WalletActivityDirection.OUTGOING
    if recipient == wallet_address:
        return WalletActivityDirection.INCOMING
    raise WalletActivityReadError("Wallet activity does not involve the requested wallet")


@dataclass(frozen=True, slots=True)
class WalletTransactionActivity:
    """Immutable transaction data needed by a wallet activity response."""

    transaction_hash: TransactionHash
    block_number: BlockNumber
    block_hash: BlockHash
    block_timestamp: datetime
    direction: WalletActivityDirection
    sender: EthereumAddress
    recipient: EthereumAddress | None
    value: Wei
    nonce: int
    gas_limit: int
    has_input: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "block_timestamp",
            _normalize_activity_timestamp(self.block_timestamp),
        )
        _validate_wallet_transaction_activity(self)

    @property
    def is_contract_creation(self) -> bool:
        """Return whether the transaction creates a contract."""
        return self.recipient is None


def _normalize_activity_timestamp(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise WalletActivityReadError("Wallet activity timestamp must be a datetime")
    try:
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise WalletActivityReadError(
                "Wallet activity timestamp must include timezone information"
            )
        normalized = value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise WalletActivityReadError("Wallet activity timestamp is invalid") from None
    if normalized < _UNIX_EPOCH:
        raise WalletActivityReadError("Wallet activity timestamp cannot precede the Unix epoch")
    return normalized


def _validate_non_negative_integer(value: int, name: str) -> None:
    if type(value) is not int:
        raise WalletActivityReadError(f"{name} must be an integer")
    if value < 0:
        raise WalletActivityReadError(f"{name} cannot be negative")


def _validate_wallet_transaction_activity(activity: WalletTransactionActivity) -> None:
    _validate_string_value_object(
        activity.transaction_hash,
        TransactionHash,
        "Wallet activity transaction hash",
        WalletActivityReadError,
    )
    _validate_integer_value_object(
        activity.block_number,
        BlockNumber,
        "Wallet activity block number",
        WalletActivityReadError,
    )
    _validate_string_value_object(
        activity.block_hash,
        BlockHash,
        "Wallet activity block hash",
        WalletActivityReadError,
    )
    normalized_timestamp = _normalize_activity_timestamp(activity.block_timestamp)
    if (
        activity.block_timestamp.tzinfo is not UTC
        or normalized_timestamp != activity.block_timestamp
    ):
        raise WalletActivityReadError("Wallet activity timestamp must be normalized to UTC")
    if not isinstance(activity.direction, WalletActivityDirection):
        raise WalletActivityReadError("Wallet activity direction is invalid")
    _validate_string_value_object(
        activity.sender,
        EthereumAddress,
        "Wallet activity sender",
        WalletActivityReadError,
    )
    if activity.recipient is not None:
        _validate_string_value_object(
            activity.recipient,
            EthereumAddress,
            "Wallet activity recipient",
            WalletActivityReadError,
        )
    _validate_integer_value_object(
        activity.value,
        Wei,
        "Wallet activity value",
        WalletActivityReadError,
    )
    _validate_non_negative_integer(activity.nonce, "Wallet activity nonce")
    _validate_non_negative_integer(activity.gas_limit, "Wallet activity gas limit")
    if type(activity.has_input) is not bool:
        raise WalletActivityReadError("Wallet activity input marker must be a boolean")


@dataclass(frozen=True, slots=True)
class WalletActivityCoverage:
    """Indexed Mainnet range available to a wallet activity reader."""

    chain_id: ChainId
    indexed_from_block: BlockNumber
    indexed_through_block: BlockNumber | None
    indexed_through_hash: BlockHash | None

    def __post_init__(self) -> None:
        _validate_wallet_activity_coverage(self)


def _validate_wallet_activity_coverage(coverage: WalletActivityCoverage) -> None:
    _validate_integer_value_object(
        coverage.chain_id,
        ChainId,
        "Wallet activity coverage chain ID",
        WalletActivityReadError,
    )
    if coverage.chain_id != ChainId.ethereum_mainnet():
        raise WalletActivityReadError("Wallet activity coverage must identify Ethereum Mainnet")
    _validate_integer_value_object(
        coverage.indexed_from_block,
        BlockNumber,
        "Wallet activity coverage start block",
        WalletActivityReadError,
    )
    if (coverage.indexed_through_block is None) != (coverage.indexed_through_hash is None):
        raise WalletActivityReadError(
            "Wallet activity coverage block and hash must both be present or absent"
        )
    if coverage.indexed_through_block is not None:
        _validate_integer_value_object(
            coverage.indexed_through_block,
            BlockNumber,
            "Wallet activity coverage end block",
            WalletActivityReadError,
        )
        _validate_string_value_object(
            coverage.indexed_through_hash,
            BlockHash,
            "Wallet activity coverage end hash",
            WalletActivityReadError,
        )
        if coverage.indexed_through_block.value < coverage.indexed_from_block.value:
            raise WalletActivityReadError("Wallet activity coverage end cannot precede its start")


@dataclass(frozen=True, slots=True)
class WalletActivityCursor:
    """Stable keyset position within one indexed wallet-activity snapshot."""

    version: int
    wallet_address: EthereumAddress
    direction: WalletActivityDirection | None
    snapshot_block_number: BlockNumber
    snapshot_block_hash: BlockHash
    last_block_number: BlockNumber
    last_transaction_hash: TransactionHash

    def __post_init__(self) -> None:
        _validate_wallet_activity_cursor(self, error_type=InvalidWalletActivityCursorError)


def _validate_wallet_activity_cursor(
    cursor: WalletActivityCursor,
    *,
    error_type: _ErrorType,
) -> None:
    if type(cursor.version) is not int or cursor.version != _CURSOR_VERSION:
        raise error_type("Wallet activity cursor version is unsupported")
    _validate_string_value_object(
        cursor.wallet_address,
        EthereumAddress,
        "Wallet activity cursor address",
        error_type,
    )
    _validate_optional_direction(cursor.direction, error_type=error_type)
    _validate_integer_value_object(
        cursor.snapshot_block_number,
        BlockNumber,
        "Wallet activity cursor snapshot block",
        error_type,
    )
    _validate_string_value_object(
        cursor.snapshot_block_hash,
        BlockHash,
        "Wallet activity cursor snapshot hash",
        error_type,
    )
    _validate_integer_value_object(
        cursor.last_block_number,
        BlockNumber,
        "Wallet activity cursor last block",
        error_type,
    )
    _validate_string_value_object(
        cursor.last_transaction_hash,
        TransactionHash,
        "Wallet activity cursor last transaction hash",
        error_type,
    )
    if cursor.last_block_number.value > cursor.snapshot_block_number.value:
        raise error_type("Wallet activity cursor last block cannot exceed its snapshot block")


@dataclass(frozen=True, slots=True)
class WalletActivityQuery:
    """Validated request for one page of wallet activity."""

    wallet_address: EthereumAddress
    limit: int = 50
    direction: WalletActivityDirection | None = None
    cursor: WalletActivityCursor | None = None

    def __post_init__(self) -> None:
        _validate_wallet_activity_query(self)


def _validate_wallet_activity_query(query: WalletActivityQuery) -> None:
    _validate_string_value_object(
        query.wallet_address,
        EthereumAddress,
        "Wallet activity query address",
        InvalidWalletActivityQueryError,
    )
    if type(query.limit) is not int or not 1 <= query.limit <= 100:
        raise InvalidWalletActivityQueryError(
            "Wallet activity query limit must be an integer from 1 through 100"
        )
    _validate_optional_direction(
        query.direction,
        error_type=InvalidWalletActivityQueryError,
    )
    if query.cursor is not None:
        if not isinstance(query.cursor, WalletActivityCursor):
            raise InvalidWalletActivityQueryError("Wallet activity query cursor is invalid")
        _validate_wallet_activity_cursor(
            query.cursor,
            error_type=InvalidWalletActivityQueryError,
        )
        if (
            query.cursor.wallet_address != query.wallet_address
            or query.cursor.direction != query.direction
        ):
            raise InvalidWalletActivityQueryError("Wallet activity cursor does not match the query")


@dataclass(frozen=True, slots=True)
class WalletActivityPage:
    """One immutable, validated page of indexed wallet activity."""

    wallet_address: EthereumAddress
    direction: WalletActivityDirection | None
    coverage: WalletActivityCoverage
    activities: tuple[WalletTransactionActivity, ...]
    next_cursor: WalletActivityCursor | None

    def __post_init__(self) -> None:
        _validate_wallet_activity_page(self)


def _validate_wallet_activity_page(page: WalletActivityPage) -> None:
    _validate_string_value_object(
        page.wallet_address,
        EthereumAddress,
        "Wallet activity page address",
        WalletActivityReadError,
    )
    _validate_optional_direction(page.direction, error_type=WalletActivityReadError)
    if not isinstance(page.coverage, WalletActivityCoverage):
        raise WalletActivityReadError("Wallet activity page coverage is invalid")
    _validate_wallet_activity_coverage(page.coverage)
    if type(page.activities) is not tuple:
        raise WalletActivityReadError("Wallet activity page activities must be a tuple")
    if page.next_cursor is not None:
        if not isinstance(page.next_cursor, WalletActivityCursor):
            raise WalletActivityReadError("Wallet activity page next cursor is invalid")
        _validate_wallet_activity_cursor(
            page.next_cursor,
            error_type=WalletActivityReadError,
        )

    seen_hashes: set[TransactionHash] = set()
    previous_key: tuple[int, str] | None = None
    for activity in page.activities:
        if not isinstance(activity, WalletTransactionActivity):
            raise WalletActivityReadError(
                "Wallet activity page must contain wallet transaction activities"
            )
        _validate_wallet_transaction_activity(activity)
        if activity.transaction_hash in seen_hashes:
            raise WalletActivityReadError("Wallet activity page contains duplicate transactions")
        seen_hashes.add(activity.transaction_hash)

        key = (activity.block_number.value, activity.transaction_hash.value)
        if previous_key is not None and previous_key <= key:
            raise WalletActivityReadError(
                "Wallet activity page must use strict descending transaction order"
            )
        previous_key = key

        expected_direction = classify_wallet_activity_direction(
            page.wallet_address,
            activity.sender,
            activity.recipient,
        )
        if activity.direction != expected_direction:
            raise WalletActivityReadError("Wallet activity direction is inconsistent")
        if page.direction is not None and activity.direction != page.direction:
            raise WalletActivityReadError("Wallet activity does not match the page filter")

        through_block = page.coverage.indexed_through_block
        if (
            activity.block_number.value < page.coverage.indexed_from_block.value
            or through_block is None
            or activity.block_number.value > through_block.value
        ):
            raise WalletActivityReadError("Wallet activity lies outside indexed coverage")
        if (
            activity.block_number == through_block
            and activity.block_hash != page.coverage.indexed_through_hash
        ):
            raise WalletActivityReadError(
                "Wallet activity block identity conflicts with indexed coverage"
            )

    if not page.activities:
        if page.next_cursor is not None:
            raise WalletActivityReadError("An empty wallet activity page cannot have a cursor")
        return

    if page.next_cursor is None:
        return

    through_block = page.coverage.indexed_through_block
    through_hash = page.coverage.indexed_through_hash
    final_activity = page.activities[-1]
    if (
        through_block is None
        or through_hash is None
        or page.next_cursor.wallet_address != page.wallet_address
        or page.next_cursor.direction != page.direction
        or page.next_cursor.snapshot_block_number != through_block
        or page.next_cursor.snapshot_block_hash != through_hash
        or page.next_cursor.last_block_number != final_activity.block_number
        or page.next_cursor.last_transaction_hash != final_activity.transaction_hash
    ):
        raise WalletActivityReadError(
            "Wallet activity next cursor does not match the returned page"
        )


class WalletActivityReader(Protocol):
    """Read port for indexed wallet activity."""

    async def read_page(self, query: WalletActivityQuery) -> WalletActivityPage: ...


class ListWalletActivity:
    """Return one validated page through the configured wallet activity read port."""

    def __init__(self, reader: WalletActivityReader) -> None:
        self._reader = reader

    async def execute(self, query: WalletActivityQuery) -> WalletActivityPage:
        if not isinstance(query, WalletActivityQuery):
            raise InvalidWalletActivityQueryError("Wallet activity query is invalid")
        try:
            _validate_wallet_activity_query(query)
        except AttributeError:
            raise InvalidWalletActivityQueryError("Wallet activity query is invalid") from None

        page = await self._reader.read_page(query)
        if not isinstance(page, WalletActivityPage):
            raise WalletActivityReadError("Wallet activity reader returned an invalid page")
        try:
            _validate_wallet_activity_page(page)
        except AttributeError:
            raise WalletActivityReadError(
                "Wallet activity reader returned an invalid page"
            ) from None
        if page.wallet_address != query.wallet_address or page.direction != query.direction:
            raise WalletActivityReadError("Wallet activity reader returned a mismatched page")
        if len(page.activities) > query.limit:
            raise WalletActivityReadError("Wallet activity reader exceeded the query limit")
        return page


class _DuplicateCursorKeyError(ValueError):
    """Internal signal used to reject ambiguous JSON objects."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateCursorKeyError
        result[key] = value
    return result


def encode_wallet_activity_cursor(cursor: WalletActivityCursor) -> str:
    """Encode a cursor as deterministic compact JSON in unpadded URL-safe Base64."""
    if not isinstance(cursor, WalletActivityCursor):
        raise InvalidWalletActivityCursorError("Wallet activity cursor is invalid")
    try:
        _validate_wallet_activity_cursor(
            cursor,
            error_type=InvalidWalletActivityCursorError,
        )
    except AttributeError:
        raise InvalidWalletActivityCursorError("Wallet activity cursor is invalid") from None
    if (
        cursor.snapshot_block_number.value >= _MAX_CURSOR_INTEGER
        or cursor.last_block_number.value >= _MAX_CURSOR_INTEGER
    ):
        raise InvalidWalletActivityCursorError(
            "Wallet activity cursor exceeds the maximum encoded length"
        )

    payload = {
        "a": cursor.wallet_address.value,
        "d": cursor.direction.value if cursor.direction is not None else "all",
        "lb": cursor.last_block_number.value,
        "lh": cursor.last_transaction_hash.value,
        "sb": cursor.snapshot_block_number.value,
        "sh": cursor.snapshot_block_hash.value,
        "v": cursor.version,
    }
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (OverflowError, TypeError, ValueError):
        raise InvalidWalletActivityCursorError("Wallet activity cursor is invalid") from None
    encoded = base64.urlsafe_b64encode(serialized).rstrip(b"=").decode("ascii")
    if len(encoded) > _MAX_CURSOR_LENGTH:
        raise InvalidWalletActivityCursorError(
            "Wallet activity cursor exceeds the maximum encoded length"
        )
    return encoded


def decode_wallet_activity_cursor(encoded: str) -> WalletActivityCursor:
    """Decode and validate the public wallet activity cursor format."""
    if not isinstance(encoded, str):
        raise InvalidWalletActivityCursorError("Wallet activity cursor must be a string")
    if len(encoded) > _MAX_CURSOR_LENGTH:
        raise InvalidWalletActivityCursorError(
            "Wallet activity cursor exceeds the maximum encoded length"
        )
    try:
        ascii_cursor = encoded.encode("ascii")
    except UnicodeEncodeError:
        raise InvalidWalletActivityCursorError("Wallet activity cursor is invalid") from None
    if not encoded or _CURSOR_ALPHABET.fullmatch(encoded) is None or len(encoded) % 4 == 1:
        raise InvalidWalletActivityCursorError("Wallet activity cursor is invalid")

    padding = b"=" * ((4 - len(ascii_cursor) % 4) % 4)
    try:
        decoded_bytes = base64.b64decode(
            ascii_cursor + padding,
            altchars=b"-_",
            validate=True,
        )
        if base64.urlsafe_b64encode(decoded_bytes).rstrip(b"=") != ascii_cursor:
            raise InvalidWalletActivityCursorError("Wallet activity cursor is invalid")
        decoded = decoded_bytes.decode("utf-8")
        parsed = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateCursorKeyError,
    ):
        raise InvalidWalletActivityCursorError("Wallet activity cursor is invalid") from None

    if type(parsed) is not dict:
        raise InvalidWalletActivityCursorError("Wallet activity cursor is invalid")
    payload = cast(dict[str, object], parsed)
    if frozenset(payload) != _CURSOR_KEYS:
        raise InvalidWalletActivityCursorError("Wallet activity cursor fields are invalid")

    version = payload["v"]
    address = payload["a"]
    direction_value = payload["d"]
    snapshot_block = payload["sb"]
    snapshot_hash = payload["sh"]
    last_block = payload["lb"]
    last_hash = payload["lh"]
    if (
        type(version) is not int
        or type(address) is not str
        or type(direction_value) is not str
        or type(snapshot_block) is not int
        or type(snapshot_hash) is not str
        or type(last_block) is not int
        or type(last_hash) is not str
    ):
        raise InvalidWalletActivityCursorError("Wallet activity cursor field types are invalid")
    if version != _CURSOR_VERSION:
        raise InvalidWalletActivityCursorError("Wallet activity cursor version is unsupported")

    try:
        direction = None if direction_value == "all" else WalletActivityDirection(direction_value)
        return WalletActivityCursor(
            version=version,
            wallet_address=EthereumAddress(address),
            direction=direction,
            snapshot_block_number=BlockNumber(snapshot_block),
            snapshot_block_hash=BlockHash(snapshot_hash),
            last_block_number=BlockNumber(last_block),
            last_transaction_hash=TransactionHash(last_hash),
        )
    except (DomainValidationError, ValueError):
        raise InvalidWalletActivityCursorError("Wallet activity cursor is invalid") from None
