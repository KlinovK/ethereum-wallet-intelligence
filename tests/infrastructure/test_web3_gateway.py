"""Tests for the async web3.py blockchain adapter."""

import asyncio
from collections.abc import Coroutine
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from hexbytes import HexBytes
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import BlockNotFound, ProviderConnectionError, TransactionNotFound

from ethereum_wallet_intelligence.application import (
    BlockBundle,
    BlockchainGateway,
    BlockchainProviderError,
    BlockchainResourceNotFoundError,
)
from ethereum_wallet_intelligence.domain.ethereum import (
    BlockHash,
    BlockNumber,
    ChainId,
    EthereumAddress,
    TransactionHash,
    Wei,
)
from ethereum_wallet_intelligence.infrastructure import Web3BlockchainGateway

BLOCK_HASH = f"0x{'ab' * 32}"
PARENT_HASH = f"0x{'cd' * 32}"
TRANSACTION_HASH = f"0x{'ef' * 32}"
TRANSACTION_HASH_2 = f"0x{'12' * 32}"
SENDER = "0x1111111111111111111111111111111111111111"
RECIPIENT = "0x2222222222222222222222222222222222222222"


async def _resolve(value: object) -> object:
    if isinstance(value, Exception):
        raise value
    return value


class StubAsyncEth:
    def __init__(self) -> None:
        self.chain_id_result: object = 1
        self.block_number_result: object = 20_000_000
        self.block_result: object = {
            "number": 20_000_000,
            "hash": HexBytes(BLOCK_HASH),
            "parentHash": HexBytes(PARENT_HASH),
            "timestamp": 1_704_067_200,
            "transactions": [],
        }
        self.balance_result: object = 10**18
        self.transaction_result: object = {
            "hash": HexBytes(TRANSACTION_HASH),
            "blockNumber": 20_000_000,
            "from": SENDER,
            "to": RECIPIENT,
            "value": 10**18,
            "nonce": 7,
            "gas": 21_000,
            "input": HexBytes("0xabcdef"),
        }
        self.receipt_result: object = {
            "transactionHash": HexBytes(TRANSACTION_HASH),
            "blockNumber": 20_000_000,
            "status": 1,
            "gasUsed": 20_000,
        }
        self.block_requests: list[tuple[object, bool]] = []
        self.balance_requests: list[object] = []
        self.transaction_requests: list[object] = []
        self.receipt_requests: list[object] = []

    @property
    def chain_id(self) -> Coroutine[Any, Any, object]:
        return _resolve(self.chain_id_result)

    @property
    def block_number(self) -> Coroutine[Any, Any, object]:
        return _resolve(self.block_number_result)

    async def get_block(
        self,
        block_number: object,
        full_transactions: bool = False,
    ) -> object:
        self.block_requests.append((block_number, full_transactions))
        return await _resolve(self.block_result)

    async def get_balance(self, address: object) -> object:
        self.balance_requests.append(address)
        return await _resolve(self.balance_result)

    async def get_transaction(self, transaction_hash: object) -> object:
        self.transaction_requests.append(transaction_hash)
        return await _resolve(self.transaction_result)

    async def get_transaction_receipt(self, transaction_hash: object) -> object:
        self.receipt_requests.append(transaction_hash)
        return await _resolve(self.receipt_result)


class StubAsyncWeb3:
    def __init__(self, eth: StubAsyncEth) -> None:
        self.eth = eth

    @staticmethod
    def to_checksum_address(address: str) -> str:
        return AsyncWeb3.to_checksum_address(address)


def make_gateway(eth: StubAsyncEth) -> Web3BlockchainGateway:
    web3 = cast(AsyncWeb3[AsyncHTTPProvider], StubAsyncWeb3(eth))
    return Web3BlockchainGateway(web3)


def get_block_payload(eth: StubAsyncEth) -> dict[str, object]:
    return cast(dict[str, object], eth.block_result)


def make_transaction_payload(
    *,
    transaction_hash: str = TRANSACTION_HASH,
    block_number: int = 20_000_000,
    recipient: str | None = RECIPIENT,
    nonce: int = 7,
) -> dict[str, object]:
    return {
        "hash": HexBytes(transaction_hash),
        "blockNumber": block_number,
        "from": SENDER,
        "to": recipient,
        "value": 10**18,
        "nonce": nonce,
        "gas": 21_000,
        "input": HexBytes("0xabcdef"),
    }


def test_adapter_satisfies_application_gateway_protocol() -> None:
    gateway: BlockchainGateway = make_gateway(StubAsyncEth())

    assert isinstance(gateway, Web3BlockchainGateway)


def test_maps_chain_id() -> None:
    gateway = make_gateway(StubAsyncEth())

    result = asyncio.run(gateway.get_chain_id())

    assert result == ChainId.ethereum_mainnet()


def test_maps_current_block_number() -> None:
    gateway = make_gateway(StubAsyncEth())

    result = asyncio.run(gateway.get_current_block_number())

    assert result == BlockNumber(20_000_000)


def test_maps_block_and_hexbytes_hashes() -> None:
    eth = StubAsyncEth()
    gateway = make_gateway(eth)

    result = asyncio.run(gateway.get_block(BlockNumber(20_000_000)))

    assert result.number == BlockNumber(20_000_000)
    assert result.hash == BlockHash(BLOCK_HASH)
    assert result.parent_hash == BlockHash(PARENT_HASH)
    assert result.timestamp == datetime(2024, 1, 1, tzinfo=UTC)
    assert eth.block_requests == [(20_000_000, False)]


def test_maps_block_with_transactions_in_one_full_block_request() -> None:
    eth = StubAsyncEth()
    get_block_payload(eth)["transactions"] = [make_transaction_payload()]
    gateway = make_gateway(eth)

    result = asyncio.run(gateway.get_block_with_transactions(BlockNumber(20_000_000)))

    assert isinstance(result, BlockBundle)
    assert result.block.number == BlockNumber(20_000_000)
    assert len(result.transactions) == 1
    assert result.transactions[0].hash == TransactionHash(TRANSACTION_HASH)
    assert eth.block_requests == [(20_000_000, True)]


def test_maps_empty_block_bundle() -> None:
    eth = StubAsyncEth()
    gateway = make_gateway(eth)

    result = asyncio.run(gateway.get_block_with_transactions(BlockNumber(20_000_000)))

    assert result.transactions == ()


def test_block_bundle_is_immutable() -> None:
    gateway = make_gateway(StubAsyncEth())
    result = asyncio.run(gateway.get_block_with_transactions(BlockNumber(20_000_000)))

    with pytest.raises(FrozenInstanceError):
        result.transactions = ()  # type: ignore[misc]


def test_maps_multiple_block_transactions() -> None:
    eth = StubAsyncEth()
    get_block_payload(eth)["transactions"] = [
        make_transaction_payload(),
        make_transaction_payload(transaction_hash=TRANSACTION_HASH_2, nonce=8),
    ]
    gateway = make_gateway(eth)

    result = asyncio.run(gateway.get_block_with_transactions(BlockNumber(20_000_000)))

    assert tuple(transaction.hash for transaction in result.transactions) == (
        TransactionHash(TRANSACTION_HASH),
        TransactionHash(TRANSACTION_HASH_2),
    )


def test_maps_contract_creation_in_block_bundle() -> None:
    eth = StubAsyncEth()
    get_block_payload(eth)["transactions"] = [make_transaction_payload(recipient=None)]
    gateway = make_gateway(eth)

    result = asyncio.run(gateway.get_block_with_transactions(BlockNumber(20_000_000)))

    assert result.transactions[0].to is None


def test_rejects_duplicate_transaction_hashes_in_block_bundle() -> None:
    eth = StubAsyncEth()
    get_block_payload(eth)["transactions"] = [
        make_transaction_payload(),
        make_transaction_payload(nonce=8),
    ]
    gateway = make_gateway(eth)

    with pytest.raises(BlockchainProviderError) as error:
        asyncio.run(gateway.get_block_with_transactions(BlockNumber(20_000_000)))

    assert isinstance(error.value.__cause__, ValueError)
    assert "duplicate transaction hashes" in str(error.value.__cause__)


def test_rejects_returned_block_number_different_from_request() -> None:
    eth = StubAsyncEth()
    gateway = make_gateway(eth)

    with pytest.raises(BlockchainProviderError) as error:
        asyncio.run(gateway.get_block_with_transactions(BlockNumber(19_999_999)))

    assert isinstance(error.value.__cause__, ValueError)
    assert "different block number" in str(error.value.__cause__)


def test_rejects_transaction_associated_with_different_block() -> None:
    eth = StubAsyncEth()
    get_block_payload(eth)["transactions"] = [make_transaction_payload(block_number=19_999_999)]
    gateway = make_gateway(eth)

    with pytest.raises(BlockchainProviderError) as error:
        asyncio.run(gateway.get_block_with_transactions(BlockNumber(20_000_000)))

    assert isinstance(error.value.__cause__, ValueError)
    assert "different block" in str(error.value.__cause__)


@pytest.mark.parametrize(
    "invalid_transactions",
    [
        None,
        "not-a-transaction-sequence",
        [HexBytes(TRANSACTION_HASH)],
        [{"hash": HexBytes(TRANSACTION_HASH)}],
    ],
)
def test_rejects_invalid_or_incomplete_block_transaction_payloads(
    invalid_transactions: object,
) -> None:
    eth = StubAsyncEth()
    get_block_payload(eth)["transactions"] = invalid_transactions
    gateway = make_gateway(eth)

    with pytest.raises(BlockchainProviderError):
        asyncio.run(gateway.get_block_with_transactions(BlockNumber(20_000_000)))


def test_rejects_block_payload_without_transactions_field() -> None:
    eth = StubAsyncEth()
    del get_block_payload(eth)["transactions"]
    gateway = make_gateway(eth)

    with pytest.raises(BlockchainProviderError) as error:
        asyncio.run(gateway.get_block_with_transactions(BlockNumber(20_000_000)))

    assert isinstance(error.value.__cause__, KeyError)


def test_translates_full_block_provider_failure_without_exposing_endpoint() -> None:
    eth = StubAsyncEth()
    provider_error = ProviderConnectionError(
        "Could not connect to https://user:super-secret@ethereum.example"
    )
    eth.block_result = provider_error
    gateway = make_gateway(eth)

    with pytest.raises(BlockchainProviderError) as error:
        asyncio.run(gateway.get_block_with_transactions(BlockNumber(20_000_000)))

    assert "super-secret" not in str(error.value)
    assert error.value.__cause__ is provider_error


def test_translates_full_block_not_found() -> None:
    eth = StubAsyncEth()
    eth.block_result = BlockNotFound("missing")
    gateway = make_gateway(eth)

    with pytest.raises(BlockchainResourceNotFoundError, match="Block 42 was not found"):
        asyncio.run(gateway.get_block_with_transactions(BlockNumber(42)))


def test_translates_chain_id_provider_failure_without_exposing_endpoint() -> None:
    eth = StubAsyncEth()
    provider_error = ProviderConnectionError(
        "Could not connect to https://user:super-secret@ethereum.example"
    )
    eth.chain_id_result = provider_error
    gateway = make_gateway(eth)

    with pytest.raises(BlockchainProviderError) as error:
        asyncio.run(gateway.get_chain_id())

    assert "super-secret" not in str(error.value)
    assert error.value.__cause__ is provider_error


def test_rejects_invalid_chain_id_payload() -> None:
    eth = StubAsyncEth()
    eth.chain_id_result = False
    gateway = make_gateway(eth)

    with pytest.raises(BlockchainProviderError) as error:
        asyncio.run(gateway.get_chain_id())

    assert isinstance(error.value.__cause__, ValueError)


def test_maps_eth_balance() -> None:
    eth = StubAsyncEth()
    gateway = make_gateway(eth)

    result = asyncio.run(gateway.get_balance(EthereumAddress(SENDER)))

    assert result == Wei(10**18)
    assert eth.balance_requests == [AsyncWeb3.to_checksum_address(SENDER)]


def test_maps_normal_transaction_and_calldata() -> None:
    eth = StubAsyncEth()
    gateway = make_gateway(eth)

    result = asyncio.run(gateway.get_transaction(TransactionHash(TRANSACTION_HASH)))

    assert result.hash == TransactionHash(TRANSACTION_HASH)
    assert result.block_number == BlockNumber(20_000_000)
    assert result.sender == EthereumAddress(SENDER)
    assert result.to == EthereumAddress(RECIPIENT)
    assert result.value == Wei(10**18)
    assert result.nonce == 7
    assert result.gas_limit == 21_000
    assert result.input_data == bytes.fromhex("abcdef")
    assert eth.transaction_requests == [TRANSACTION_HASH]


def test_maps_contract_creation_and_hex_string_calldata() -> None:
    eth = StubAsyncEth()
    eth.transaction_result = {
        "hash": TRANSACTION_HASH.upper().replace("0X", "0x"),
        "blockNumber": 20_000_000,
        "from": SENDER,
        "to": None,
        "value": 0,
        "nonce": 8,
        "gas": 100_000,
        "input": "0x60006000f3",
    }
    gateway = make_gateway(eth)

    result = asyncio.run(gateway.get_transaction(TransactionHash(TRANSACTION_HASH)))

    assert result.to is None
    assert result.value == Wei(0)
    assert result.input_data == bytes.fromhex("60006000f3")


@pytest.mark.parametrize(("status", "succeeded"), [(1, True), (0, False)])
def test_maps_successful_and_failed_receipts(status: int, succeeded: bool) -> None:
    eth = StubAsyncEth()
    eth.receipt_result = {
        "transactionHash": HexBytes(TRANSACTION_HASH),
        "blockNumber": 20_000_000,
        "status": status,
        "gasUsed": 19_876,
    }
    gateway = make_gateway(eth)

    result = asyncio.run(gateway.get_transaction_receipt(TransactionHash(TRANSACTION_HASH)))

    assert result.transaction_hash == TransactionHash(TRANSACTION_HASH)
    assert result.block_number == BlockNumber(20_000_000)
    assert result.succeeded is succeeded
    assert result.gas_used == 19_876
    assert eth.receipt_requests == [TRANSACTION_HASH]


def test_translates_malformed_provider_data() -> None:
    eth = StubAsyncEth()
    eth.receipt_result = {
        "transactionHash": HexBytes(TRANSACTION_HASH),
        "blockNumber": 20_000_000,
        "status": 2,
        "gasUsed": 20_000,
    }
    gateway = make_gateway(eth)

    with pytest.raises(BlockchainProviderError, match="failed to return") as error:
        asyncio.run(gateway.get_transaction_receipt(TransactionHash(TRANSACTION_HASH)))

    assert isinstance(error.value.__cause__, ValueError)


def test_translates_block_not_found() -> None:
    eth = StubAsyncEth()
    eth.block_result = BlockNotFound("missing")
    gateway = make_gateway(eth)

    with pytest.raises(BlockchainResourceNotFoundError, match="Block 42 was not found") as error:
        asyncio.run(gateway.get_block(BlockNumber(42)))

    assert isinstance(error.value.__cause__, BlockNotFound)


def test_translates_transaction_not_found() -> None:
    eth = StubAsyncEth()
    eth.transaction_result = TransactionNotFound("missing")
    gateway = make_gateway(eth)

    with pytest.raises(BlockchainResourceNotFoundError, match="Transaction was not found"):
        asyncio.run(gateway.get_transaction(TransactionHash(TRANSACTION_HASH)))


def test_translates_provider_failure_without_exposing_endpoint() -> None:
    eth = StubAsyncEth()
    provider_error = ProviderConnectionError(
        "Could not connect to https://user:super-secret@ethereum.example"
    )
    eth.block_number_result = provider_error
    gateway = make_gateway(eth)

    with pytest.raises(BlockchainProviderError) as error:
        asyncio.run(gateway.get_current_block_number())

    assert "super-secret" not in str(error.value)
    assert error.value.__cause__ is provider_error


@pytest.mark.parametrize("rpc_url", ["", "ftp://ethereum.example", "not-a-url"])
def test_rejects_invalid_rpc_url_when_constructing_adapter(rpc_url: str) -> None:
    with pytest.raises(ValueError, match=r"valid HTTP\(S\) URL"):
        Web3BlockchainGateway.from_rpc_url(rpc_url)
