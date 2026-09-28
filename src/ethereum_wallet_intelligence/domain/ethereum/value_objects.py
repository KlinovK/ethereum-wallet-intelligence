"""Framework-independent Ethereum value objects."""

from dataclasses import dataclass
from decimal import Decimal
from string import hexdigits
from typing import Self


class DomainValidationError(ValueError):
    """Raised when a domain value cannot be constructed safely."""


@dataclass(frozen=True, slots=True)
class EthereumAddress:
    """A structurally valid Ethereum address in canonical lowercase form."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise DomainValidationError("Ethereum address must be a string")
        if not self.value.startswith("0x"):
            raise DomainValidationError("Ethereum address must start with '0x'")

        hexadecimal = self.value[2:]
        if len(hexadecimal) != 40:
            raise DomainValidationError(
                "Ethereum address must contain exactly 40 hexadecimal characters after '0x'"
            )
        if any(character not in hexdigits for character in hexadecimal):
            raise DomainValidationError("Ethereum address contains non-hexadecimal characters")

        object.__setattr__(self, "value", f"0x{hexadecimal.lower()}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Wei:
    """An exact, non-negative amount in Ethereum's smallest denomination."""

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise DomainValidationError("Wei value must be an integer")
        if self.value < 0:
            raise DomainValidationError("Wei value cannot be negative")

    def to_eth(self) -> Decimal:
        """Return the exact ETH value without relying on Decimal context precision."""
        return Decimal(f"{self.value}e-18")


@dataclass(frozen=True, slots=True)
class ChainId:
    """A positive EVM chain identifier."""

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise DomainValidationError("Chain ID must be an integer")
        if self.value <= 0:
            raise DomainValidationError("Chain ID must be positive")

    @classmethod
    def ethereum_mainnet(cls) -> Self:
        """Return the chain ID for Ethereum Mainnet."""
        return cls(1)
