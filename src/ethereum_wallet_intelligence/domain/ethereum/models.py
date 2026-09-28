"""Framework-independent models for indexed Ethereum blockchain data."""

from dataclasses import dataclass
from datetime import UTC, datetime

from ethereum_wallet_intelligence.domain.ethereum.value_objects import (
    BlockHash,
    BlockNumber,
    DomainValidationError,
    EthereumAddress,
    TransactionHash,
    Wei,
)

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _validate_non_negative_integer(value: int, name: str) -> None:
    if type(value) is not int:
        raise DomainValidationError(f"{name} must be an integer")
    if value < 0:
        raise DomainValidationError(f"{name} cannot be negative")


@dataclass(frozen=True, slots=True)
class Block:
    """The subset of an Ethereum block required for indexing."""

    number: BlockNumber
    hash: BlockHash
    parent_hash: BlockHash
    timestamp: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.number, BlockNumber):
            raise DomainValidationError("Block number must be a BlockNumber")
        if not isinstance(self.hash, BlockHash):
            raise DomainValidationError("Block hash must be a BlockHash")
        if not isinstance(self.parent_hash, BlockHash):
            raise DomainValidationError("Parent hash must be a BlockHash")
        if not isinstance(self.timestamp, datetime):
            raise DomainValidationError("Block timestamp must be a datetime")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise DomainValidationError("Block timestamp must include timezone information")

        normalized_timestamp = self.timestamp.astimezone(UTC)
        if normalized_timestamp < _UNIX_EPOCH:
            raise DomainValidationError("Block timestamp cannot be before the Unix epoch")

        object.__setattr__(self, "timestamp", normalized_timestamp)


@dataclass(frozen=True, slots=True)
class Transaction:
    """The subset of an Ethereum transaction required for wallet indexing."""

    hash: TransactionHash
    block_number: BlockNumber
    sender: EthereumAddress
    to: EthereumAddress | None
    value: Wei
    nonce: int
    gas_limit: int
    input_data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.hash, TransactionHash):
            raise DomainValidationError("Transaction hash must be a TransactionHash")
        if not isinstance(self.block_number, BlockNumber):
            raise DomainValidationError("Transaction block number must be a BlockNumber")
        if not isinstance(self.sender, EthereumAddress):
            raise DomainValidationError("Transaction sender must be an EthereumAddress")
        if self.to is not None and not isinstance(self.to, EthereumAddress):
            raise DomainValidationError("Transaction recipient must be an EthereumAddress or None")
        if not isinstance(self.value, Wei):
            raise DomainValidationError("Transaction value must be Wei")
        _validate_non_negative_integer(self.nonce, "Transaction nonce")
        _validate_non_negative_integer(self.gas_limit, "Transaction gas limit")
        if type(self.input_data) is not bytes:
            raise DomainValidationError("Transaction input data must be bytes")


@dataclass(frozen=True, slots=True)
class TransactionReceipt:
    """Execution information for an indexed Ethereum transaction."""

    transaction_hash: TransactionHash
    block_number: BlockNumber
    succeeded: bool
    gas_used: int

    def __post_init__(self) -> None:
        if not isinstance(self.transaction_hash, TransactionHash):
            raise DomainValidationError("Receipt transaction hash must be a TransactionHash")
        if not isinstance(self.block_number, BlockNumber):
            raise DomainValidationError("Receipt block number must be a BlockNumber")
        if type(self.succeeded) is not bool:
            raise DomainValidationError("Receipt execution status must be a boolean")
        _validate_non_negative_integer(self.gas_used, "Receipt gas used")
