"""Tests for the async web3.py blockchain adapter."""

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from hexbytes import HexBytes
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import BlockNotFound, ProviderConnectionError, TransactionNotFound

from ethereum_wallet_intelligence.application import (
    BlockchainGateway,
    BlockchainProviderError,
    BlockchainResourceNotFoundError,
)
from ethereum_wallet_intelligence.domain.ethereum import (
    BlockHash,
    BlockNumber,
    EthereumAddress,
    TransactionHash,
    Wei,
)
from ethereum_wallet_intelligence.infrastructure import Web3BlockchainGateway

BLOCK_HASH = f"0x{'ab' * 32}"
PARENT_HASH = f"0x{'cd' * 32}"
TRANSACTION_HASH = f"0x{'ef' * 32}"
SENDER = "0x1111111111111111111111111111111111111111"
RECIPIENT = "0x2222222222222222222222222222222222222222"


async def _resolve(value: object) -> object:
    if isinstance(value, Exception):
        raise value
    return value


class StubAsyncEth:
    def __init__(self) -> None:
        self.block_number_result: object = 20_000_000
        self.block_result: object = {
            "number": 20_000_000,
            "hash": HexBytes(BLOCK_HASH),
            "parentHash": HexBytes(PARENT_HASH),
            "timestamp": 1_704_067_200,
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
        self.block_requests: list[object] = []
        self.balance_requests: list[object] = []
        self.transaction_requests: list[object] = []
        self.receipt_requests: list[object] = []

    @property
    def block_number(self) -> Coroutine[Any, Any, object]:
        return _resolve(self.block_number_result)

    async def get_block(self, block_number: object) -> object:
        self.block_requests.append(block_number)
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


def test_adapter_satisfies_application_gateway_protocol() -> None:
    gateway: BlockchainGateway = make_gateway(StubAsyncEth())

    assert isinstance(gateway, Web3BlockchainGateway)


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
    assert eth.block_requests == [20_000_000]


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
