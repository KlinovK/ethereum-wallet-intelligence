"""Tests for Ethereum domain value objects."""

from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext

import pytest

from ethereum_wallet_intelligence.domain.ethereum import (
    ChainId,
    DomainValidationError,
    EthereumAddress,
    Wei,
)


@pytest.mark.parametrize(
    "value_object",
    [
        EthereumAddress("0x1234567890abcdef1234567890abcdef12345678"),
        Wei(1),
        ChainId(1),
    ],
)
def test_value_objects_are_immutable(value_object: object) -> None:
    with pytest.raises(FrozenInstanceError):
        value_object.value = "replacement"  # type: ignore[attr-defined]


class TestEthereumAddress:
    def test_accepts_valid_lowercase_address(self) -> None:
        raw_address = "0x1234567890abcdef1234567890abcdef12345678"

        address = EthereumAddress(raw_address)

        assert address.value == raw_address
        assert str(address) == raw_address

    def test_accepts_and_normalizes_uppercase_hexadecimal_characters(self) -> None:
        address = EthereumAddress("0xABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD")

        assert address.value == "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"

    def test_equivalent_addresses_are_equal_after_normalization(self) -> None:
        lowercase = EthereumAddress("0xabcdefabcdefabcdefabcdefabcdefabcdefabcd")
        uppercase = EthereumAddress("0xABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD")

        assert lowercase == uppercase

    def test_rejects_missing_prefix(self) -> None:
        with pytest.raises(DomainValidationError, match="must start with '0x'"):
            EthereumAddress("abcdefabcdefabcdefabcdefabcdefabcdefabcd")

    @pytest.mark.parametrize(
        "raw_address",
        [
            "0x1234567890abcdef1234567890abcdef1234567",
            "0x1234567890abcdef1234567890abcdef123456789",
        ],
    )
    def test_rejects_wrong_length(self, raw_address: str) -> None:
        with pytest.raises(DomainValidationError, match="exactly 40"):
            EthereumAddress(raw_address)

    def test_rejects_non_hexadecimal_characters(self) -> None:
        with pytest.raises(DomainValidationError, match="non-hexadecimal"):
            EthereumAddress("0xgggggggggggggggggggggggggggggggggggggggg")


class TestWei:
    def test_accepts_zero(self) -> None:
        assert Wei(0).value == 0

    def test_accepts_positive_value(self) -> None:
        assert Wei(42).value == 42

    def test_rejects_negative_value(self) -> None:
        with pytest.raises(DomainValidationError, match="cannot be negative"):
            Wei(-1)

    def test_converts_one_eth_exactly(self) -> None:
        assert Wei(10**18).to_eth() == Decimal("1")

    def test_converts_non_whole_eth_amount_exactly(self) -> None:
        with localcontext() as context:
            context.prec = 3
            converted = Wei(1_234_567_890_123_456_789).to_eth()

        assert converted == Decimal("1.234567890123456789")

    def test_preserves_very_large_integer_value(self) -> None:
        raw_value = 10**100 + 123_456_789
        amount = Wei(raw_value)

        assert amount.value == raw_value
        assert amount.to_eth() == Decimal(f"{raw_value}e-18")


class TestChainId:
    def test_represents_ethereum_mainnet(self) -> None:
        assert ChainId.ethereum_mainnet() == ChainId(1)

    def test_accepts_arbitrary_positive_chain_id(self) -> None:
        assert ChainId(11_155_111).value == 11_155_111

    def test_rejects_zero(self) -> None:
        with pytest.raises(DomainValidationError, match="must be positive"):
            ChainId(0)

    def test_rejects_negative_value(self) -> None:
        with pytest.raises(DomainValidationError, match="must be positive"):
            ChainId(-1)
