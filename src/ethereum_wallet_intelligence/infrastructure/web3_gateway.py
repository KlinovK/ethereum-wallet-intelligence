"""Async web3.py adapter for the application blockchain port."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Self, cast
from urllib.parse import urlsplit

from aiohttp import ClientConnectionError, ClientResponseError
from eth_typing import HexStr
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import (
    BlockNotFound,
    ProviderConnectionError,
    RequestTimedOut,
    TransactionNotFound,
)

from ethereum_wallet_intelligence.application import (
    BlockBundle,
    BlockchainProviderError,
    BlockchainResourceNotFoundError,
    InvalidBlockchainResponseError,
    PermanentBlockchainProviderError,
    TransientBlockchainProviderError,
)
from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockHash,
    BlockNumber,
    ChainId,
    EthereumAddress,
    Transaction,
    TransactionHash,
    TransactionReceipt,
    Wei,
)

_DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
_MIN_REQUEST_TIMEOUT_SECONDS = 0.1
_MAX_REQUEST_TIMEOUT_SECONDS = 60.0
_INVALID_RESPONSE_ERRORS = (KeyError, TypeError, ValueError, OverflowError, OSError)


def _validate_rpc_url(rpc_url: str) -> str:
    sanitized_url = rpc_url.strip()
    parsed = urlsplit(sanitized_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("Ethereum RPC URL must be a valid HTTP(S) URL")
    return sanitized_url


def _validate_request_timeout(request_timeout_seconds: float) -> float:
    if type(request_timeout_seconds) not in {int, float}:
        raise ValueError("Ethereum RPC request timeout must be a number")
    timeout = float(request_timeout_seconds)
    if not _MIN_REQUEST_TIMEOUT_SECONDS <= timeout <= _MAX_REQUEST_TIMEOUT_SECONDS:
        raise ValueError("Ethereum RPC request timeout must be between 0.1 and 60 seconds")
    return timeout


def _provider_error(error: Exception, operation: str) -> BlockchainProviderError:
    message = f"Ethereum provider failed to {operation}"
    if isinstance(error, ClientResponseError):
        if error.status == 429 or 500 <= error.status <= 599:
            return TransientBlockchainProviderError(f"{message} (HTTP {error.status})")
        return PermanentBlockchainProviderError(f"{message} (HTTP {error.status})")
    if isinstance(
        error,
        (ClientConnectionError, ProviderConnectionError, RequestTimedOut, TimeoutError),
    ):
        return TransientBlockchainProviderError(message)
    return PermanentBlockchainProviderError(message)


def _invalid_response_error(resource: str) -> InvalidBlockchainResponseError:
    return InvalidBlockchainResponseError(f"Ethereum provider returned invalid {resource} data")


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


def _as_sequence(value: object, field_name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"Provider field '{field_name}' must be a sequence")
    return cast(Sequence[object], value)


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


def _map_block_bundle(raw_block: object, requested_number: BlockNumber) -> BlockBundle:
    data = _as_mapping(raw_block, "block")
    block = _map_block(data)
    if block.number != requested_number:
        raise ValueError("Provider returned a different block number than requested")

    raw_transactions = _as_sequence(data["transactions"], "transactions")
    transactions = tuple(_map_transaction(raw_transaction) for raw_transaction in raw_transactions)
    return BlockBundle(block=block, transactions=transactions)


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
    def from_rpc_url(
        cls,
        rpc_url: str,
        *,
        request_timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> Self:
        """Construct the adapter from a validated HTTP(S) JSON-RPC endpoint."""
        endpoint = _validate_rpc_url(rpc_url)
        timeout = _validate_request_timeout(request_timeout_seconds)
        provider = AsyncHTTPProvider(
            endpoint,
            request_kwargs={"timeout": timeout},
            exception_retry_configuration=None,
        )
        return cls(AsyncWeb3(provider))

    async def get_chain_id(self) -> ChainId:
        translated_error: BlockchainProviderError
        try:
            raw_chain_id = await self._web3.eth.chain_id
        except Exception as error:
            translated_error = _provider_error(error, "return the chain ID")
        else:
            try:
                return ChainId(_as_integer(raw_chain_id, "chainId"))
            except _INVALID_RESPONSE_ERRORS:
                translated_error = _invalid_response_error("chain ID")
        raise translated_error

    async def get_current_block_number(self) -> BlockNumber:
        translated_error: BlockchainProviderError
        try:
            raw_number = await self._web3.eth.block_number
        except Exception as error:
            translated_error = _provider_error(error, "return the current block number")
        else:
            try:
                return BlockNumber(_as_integer(raw_number, "blockNumber"))
            except _INVALID_RESPONSE_ERRORS:
                translated_error = _invalid_response_error("block number")
        raise translated_error

    async def get_block(self, number: BlockNumber) -> Block:
        translated_error: BlockchainProviderError | BlockchainResourceNotFoundError
        try:
            raw_block = await self._web3.eth.get_block(number.value)
        except BlockNotFound:
            translated_error = BlockchainResourceNotFoundError(
                f"Block {number.value} was not found"
            )
        except Exception as error:
            translated_error = _provider_error(error, "return the block")
        else:
            try:
                return _map_block(raw_block)
            except _INVALID_RESPONSE_ERRORS:
                translated_error = _invalid_response_error("block")
        raise translated_error

    async def get_block_with_transactions(self, number: BlockNumber) -> BlockBundle:
        translated_error: BlockchainProviderError | BlockchainResourceNotFoundError
        try:
            raw_block = await self._web3.eth.get_block(number.value, full_transactions=True)
        except BlockNotFound:
            translated_error = BlockchainResourceNotFoundError(
                f"Block {number.value} was not found"
            )
        except Exception as error:
            translated_error = _provider_error(error, "return the block with transactions")
        else:
            try:
                return _map_block_bundle(raw_block, number)
            except _INVALID_RESPONSE_ERRORS:
                translated_error = _invalid_response_error("block with transactions")
        raise translated_error

    async def get_balance(self, address: EthereumAddress) -> Wei:
        translated_error: BlockchainProviderError
        try:
            checksum_address = self._web3.to_checksum_address(address.value)
            raw_balance = await self._web3.eth.get_balance(checksum_address)
        except Exception as error:
            translated_error = _provider_error(error, "return the balance")
        else:
            try:
                return Wei(_as_integer(raw_balance, "balance"))
            except _INVALID_RESPONSE_ERRORS:
                translated_error = _invalid_response_error("balance")
        raise translated_error

    async def get_transaction(self, transaction_hash: TransactionHash) -> Transaction:
        translated_error: BlockchainProviderError | BlockchainResourceNotFoundError
        try:
            raw_transaction = await self._web3.eth.get_transaction(HexStr(transaction_hash.value))
        except TransactionNotFound:
            translated_error = BlockchainResourceNotFoundError("Transaction was not found")
        except Exception as error:
            translated_error = _provider_error(error, "return the transaction")
        else:
            try:
                return _map_transaction(raw_transaction)
            except _INVALID_RESPONSE_ERRORS:
                translated_error = _invalid_response_error("transaction")
        raise translated_error

    async def get_transaction_receipt(
        self,
        transaction_hash: TransactionHash,
    ) -> TransactionReceipt:
        translated_error: BlockchainProviderError | BlockchainResourceNotFoundError
        try:
            raw_receipt = await self._web3.eth.get_transaction_receipt(
                HexStr(transaction_hash.value)
            )
        except TransactionNotFound:
            translated_error = BlockchainResourceNotFoundError("Transaction receipt was not found")
        except Exception as error:
            translated_error = _provider_error(error, "return the transaction receipt")
        else:
            try:
                return _map_transaction_receipt(raw_receipt)
            except _INVALID_RESPONSE_ERRORS:
                translated_error = _invalid_response_error("transaction receipt")
        raise translated_error
