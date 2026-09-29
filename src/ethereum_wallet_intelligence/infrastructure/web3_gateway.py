"""Async web3.py adapter for the application blockchain port."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Self, cast
from urllib.parse import urlsplit

from eth_typing import HexStr
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import BlockNotFound, TransactionNotFound

from ethereum_wallet_intelligence.application import (
    BlockchainProviderError,
    BlockchainResourceNotFoundError,
)
from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockHash,
    BlockNumber,
    EthereumAddress,
    Transaction,
    TransactionHash,
    TransactionReceipt,
    Wei,
)


def _validate_rpc_url(rpc_url: str) -> str:
    sanitized_url = rpc_url.strip()
    parsed = urlsplit(sanitized_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("Ethereum RPC URL must be a valid HTTP(S) URL")
    return sanitized_url


def _as_integer(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"Provider field '{field_name}' must be an integer")
    return value


def _as_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Provider field '{field_name}' must be a string")
    return value


def _as_hex_string(value: object, field_name: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return f"0x{bytes(value).hex()}"
    raise ValueError(f"Provider field '{field_name}' must be hexadecimal data")


def _as_bytes(value: object, field_name: str) -> bytes:
    if isinstance(value, bytes):
        return bytes(value)
    if isinstance(value, str) and value.startswith("0x"):
        try:
            return bytes.fromhex(value[2:])
        except ValueError as error:
            raise ValueError(
                f"Provider field '{field_name}' contains invalid hexadecimal data"
            ) from error
    raise ValueError(f"Provider field '{field_name}' must be hexadecimal data")


def _as_mapping(value: object, resource_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Provider returned invalid {resource_name} data")
    return cast(Mapping[str, object], value)


def _map_block(raw_block: object) -> Block:
    data = _as_mapping(raw_block, "block")
    return Block(
        number=BlockNumber(_as_integer(data["number"], "number")),
        hash=BlockHash(_as_hex_string(data["hash"], "hash")),
        parent_hash=BlockHash(_as_hex_string(data["parentHash"], "parentHash")),
        timestamp=datetime.fromtimestamp(_as_integer(data["timestamp"], "timestamp"), tz=UTC),
    )


def _map_transaction(raw_transaction: object) -> Transaction:
    data = _as_mapping(raw_transaction, "transaction")
    raw_recipient = data["to"]
    recipient = None if raw_recipient is None else EthereumAddress(_as_string(raw_recipient, "to"))
    return Transaction(
        hash=TransactionHash(_as_hex_string(data["hash"], "hash")),
        block_number=BlockNumber(_as_integer(data["blockNumber"], "blockNumber")),
        sender=EthereumAddress(_as_string(data["from"], "from")),
        to=recipient,
        value=Wei(_as_integer(data["value"], "value")),
        nonce=_as_integer(data["nonce"], "nonce"),
        gas_limit=_as_integer(data["gas"], "gas"),
        input_data=_as_bytes(data["input"], "input"),
    )


def _map_transaction_receipt(raw_receipt: object) -> TransactionReceipt:
    data = _as_mapping(raw_receipt, "transaction receipt")
    status = _as_integer(data["status"], "status")
    if status not in {0, 1}:
        raise ValueError("Provider field 'status' must be 0 or 1")
    return TransactionReceipt(
        transaction_hash=TransactionHash(
            _as_hex_string(data["transactionHash"], "transactionHash")
        ),
        block_number=BlockNumber(_as_integer(data["blockNumber"], "blockNumber")),
        succeeded=status == 1,
        gas_used=_as_integer(data["gasUsed"], "gasUsed"),
    )


class Web3BlockchainGateway:
    """Read Ethereum JSON-RPC data asynchronously and return domain objects."""

    def __init__(self, web3: AsyncWeb3[AsyncHTTPProvider]) -> None:
        self._web3 = web3

    @classmethod
    def from_rpc_url(cls, rpc_url: str) -> Self:
        """Construct the adapter from a validated HTTP(S) JSON-RPC endpoint."""
        endpoint = _validate_rpc_url(rpc_url)
        return cls(AsyncWeb3(AsyncHTTPProvider(endpoint)))

    async def get_current_block_number(self) -> BlockNumber:
        try:
            raw_number = await self._web3.eth.block_number
            return BlockNumber(_as_integer(raw_number, "blockNumber"))
        except Exception as error:
            raise BlockchainProviderError(
                "Ethereum provider failed to return the current block number"
            ) from error

    async def get_block(self, number: BlockNumber) -> Block:
        try:
            raw_block = await self._web3.eth.get_block(number.value)
            return _map_block(raw_block)
        except BlockNotFound as error:
            raise BlockchainResourceNotFoundError(f"Block {number.value} was not found") from error
        except Exception as error:
            raise BlockchainProviderError("Ethereum provider failed to return the block") from error

    async def get_balance(self, address: EthereumAddress) -> Wei:
        try:
            checksum_address = self._web3.to_checksum_address(address.value)
            raw_balance = await self._web3.eth.get_balance(checksum_address)
            return Wei(_as_integer(raw_balance, "balance"))
        except Exception as error:
            raise BlockchainProviderError(
                "Ethereum provider failed to return the balance"
            ) from error

    async def get_transaction(self, transaction_hash: TransactionHash) -> Transaction:
        try:
            raw_transaction = await self._web3.eth.get_transaction(HexStr(transaction_hash.value))
            return _map_transaction(raw_transaction)
        except TransactionNotFound as error:
            raise BlockchainResourceNotFoundError("Transaction was not found") from error
        except Exception as error:
            raise BlockchainProviderError(
                "Ethereum provider failed to return the transaction"
            ) from error

    async def get_transaction_receipt(
        self,
        transaction_hash: TransactionHash,
    ) -> TransactionReceipt:
        try:
            raw_receipt = await self._web3.eth.get_transaction_receipt(
                HexStr(transaction_hash.value)
            )
            return _map_transaction_receipt(raw_receipt)
        except TransactionNotFound as error:
            raise BlockchainResourceNotFoundError("Transaction receipt was not found") from error
        except Exception as error:
            raise BlockchainProviderError(
                "Ethereum provider failed to return the transaction receipt"
            ) from error
