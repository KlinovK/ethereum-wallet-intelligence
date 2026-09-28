"""Tests for value objects used by Ethereum blockchain data models."""

from dataclasses import FrozenInstanceError

import pytest

from ethereum_wallet_intelligence.domain.ethereum import (
    BlockHash,
    BlockNumber,
    DomainValidationError,
    TransactionHash,
)

type HashType = type[BlockHash] | type[TransactionHash]


class TestBlockNumber:
    def test_accepts_zero(self) -> None:
        assert BlockNumber(0).value == 0

    def test_accepts_positive_value(self) -> None:
        assert BlockNumber(20_000_000).value == 20_000_000

    def test_rejects_negative_value(self) -> None:
        with pytest.raises(DomainValidationError, match="cannot be negative"):
            BlockNumber(-1)

    @pytest.mark.parametrize("invalid_value", [True, 1.5, "1"])
    def test_rejects_boolean_and_non_integer_values(self, invalid_value: object) -> None:
        with pytest.raises(DomainValidationError, match="must be an integer"):
            BlockNumber(invalid_value)  # type: ignore[arg-type]

    def test_is_immutable(self) -> None:
        block_number = BlockNumber(1)

        with pytest.raises(FrozenInstanceError):
            block_number.value = 2  # type: ignore[misc]


@pytest.mark.parametrize("hash_type", [BlockHash, TransactionHash])
class TestHashes:
    def test_accepts_valid_hash(self, hash_type: HashType) -> None:
        raw_hash = f"0x{'ab' * 32}"

        value = hash_type(raw_hash)

        assert value.value == raw_hash
        assert str(value) == raw_hash

    def test_normalizes_uppercase_hexadecimal(self, hash_type: HashType) -> None:
        value = hash_type(f"0x{'AB' * 32}")

        assert value.value == f"0x{'ab' * 32}"

    def test_equivalent_hashes_are_equal(self, hash_type: HashType) -> None:
        assert hash_type(f"0x{'AB' * 32}") == hash_type(f"0x{'ab' * 32}")

    def test_rejects_incorrect_prefix(self, hash_type: HashType) -> None:
        with pytest.raises(DomainValidationError, match="must start with '0x'"):
            hash_type("AB" * 32)

    def test_rejects_incorrect_length(self, hash_type: HashType) -> None:
        with pytest.raises(DomainValidationError, match="exactly 64"):
            hash_type(f"0x{'a' * 63}")

    def test_rejects_non_hexadecimal_characters(self, hash_type: HashType) -> None:
        with pytest.raises(DomainValidationError, match="non-hexadecimal"):
            hash_type(f"0x{'g' * 64}")

    def test_is_immutable(self, hash_type: HashType) -> None:
        value = hash_type(f"0x{'a' * 64}")

        with pytest.raises(FrozenInstanceError):
            value.value = f"0x{'b' * 64}"  # type: ignore[misc]
