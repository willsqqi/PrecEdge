"""Recorded ask books and explicit synthetic fee schedules; no venue operations."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

from .contracts import known, timestamp


def money(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a monetary value")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid monetary value") from exc
    if not result.is_finite():
        raise ValueError("money must be finite")
    return result


def count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("quantity must be a positive whole number of contracts")
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Level:
    price: Decimal
    quantity: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", money(self.price))
        if not Decimal(0) <= self.price <= Decimal(1):
            raise ValueError("binary contract price must be between zero and one")
        count(self.quantity)

    def to_dict(self) -> dict:
        return {"price": str(self.price), "quantity": self.quantity}


@dataclass(frozen=True)
class Book:
    snapshot_id: str
    contract_key: str
    contract_fingerprint: str
    outcome: str
    observed_at: str
    asks: tuple[Level, ...]

    def __post_init__(self) -> None:
        if not known(self.snapshot_id) or not known(self.contract_key):
            raise ValueError("book requires an immutable snapshot ID and contract key")
        if len(self.contract_fingerprint) != 64 or any(c not in "0123456789abcdef" for c in self.contract_fingerprint):
            raise ValueError("book requires a SHA-256 contract fingerprint")
        if self.outcome not in {"yes", "no"}:
            raise ValueError("book outcome must be yes or no")
        timestamp(self.observed_at)
        object.__setattr__(self, "asks", tuple(self.asks))
        if any(not isinstance(level, Level) for level in self.asks):
            raise ValueError("asks must contain Level records")
        prices = [level.price for level in self.asks]
        if prices != sorted(set(prices)):
            raise ValueError("ask prices must be unique and ascending")

    @property
    def identity(self) -> tuple[str, str]:
        return self.contract_key, self.outcome

    def to_dict(self) -> dict:
        return {**asdict(self), "asks": [level.to_dict() for level in self.asks]}

    @property
    def fingerprint(self) -> str:
        return digest(self.to_dict())

    @classmethod
    def from_dict(cls, row: dict) -> Book:
        return cls(**{**row, "asks": tuple(Level(**level) for level in row["asks"])})


@dataclass(frozen=True)
class FeeSchedule:
    contract_key: str
    model: str
    rate: Decimal
    reference: str
    reviewed_at: str
    valid_until: str
    minimum: Decimal = Decimal("0")
    rounding_increment: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        for name in ("rate", "minimum", "rounding_increment"):
            object.__setattr__(self, name, money(getattr(self, name)))
        if not known(self.contract_key) or not known(self.reference):
            raise ValueError("fee schedule needs a contract key and evidence reference")
        if self.model not in {"per_contract", "quadratic"}:
            raise ValueError("unsupported fee model")
        if self.rate < 0 or self.minimum < 0 or self.rounding_increment <= 0:
            raise ValueError("invalid fee rate, minimum or rounding increment")
        if timestamp(self.valid_until) <= timestamp(self.reviewed_at):
            raise ValueError("fee validity must end after review")

    def charge(self, fills: tuple[Level, ...]) -> Decimal:
        if not fills:
            return Decimal(0)
        # One aggregate per simulated leg; this is an explicit modelling choice.
        raw = sum((self.rate * level.quantity * (
            level.price * (1 - level.price) if self.model == "quadratic" else 1
        ) for level in fills), Decimal(0))
        return (max(raw, self.minimum) / self.rounding_increment).to_integral_value(rounding=ROUND_CEILING) * self.rounding_increment

    def to_dict(self) -> dict:
        return {name: str(value) if isinstance(value, Decimal) else value for name, value in asdict(self).items()}


class DepthPool:
    """Consume visible depth once per snapshot; a new snapshot supplies new depth.

    A snapshot ID is immutable. Registering an older snapshot cannot replace the
    latest one. This assumes each new record is a full refreshed book, not a delta.
    """

    def __init__(self) -> None:
        self.books: dict[str, Book] = {}
        self.remaining: dict[str, list[int]] = {}
        self.latest: dict[tuple[str, str], str] = {}

    def register(self, book: Book) -> None:
        existing = self.books.get(book.snapshot_id)
        if existing is not None:
            if existing != book:
                raise ValueError("snapshot ID reused with changed payload")
            return
        previous = self.books.get(self.latest.get(book.identity, ""))
        if previous and timestamp(book.observed_at) <= timestamp(previous.observed_at):
            raise ValueError("new snapshot must advance observation time")
        self.books[book.snapshot_id] = book
        self.remaining[book.snapshot_id] = [level.quantity for level in book.asks]
        self.latest[book.identity] = book.snapshot_id

    def quote(self, snapshot_id: str, quantity: int) -> tuple[Level, ...]:
        count(quantity)
        book = self.books[snapshot_id]
        if self.latest[book.identity] != snapshot_id:
            raise ValueError("superseded snapshot")
        needed = quantity
        fills = []
        for level, available in zip(book.asks, self.remaining[snapshot_id], strict=True):
            take = min(needed, available)
            if take:
                fills.append(Level(level.price, take))
            needed -= take
            if not needed:
                return tuple(fills)
        raise ValueError("insufficient visible depth")

    def consume(self, legs: tuple[tuple[str, tuple[Level, ...]], ...]) -> None:
        # Stage on a copy: a failed second leg cannot consume the first leg.
        staged = {key: values.copy() for key, values in self.remaining.items()}
        for snapshot_id, fills in legs:
            book = self.books[snapshot_id]
            if self.latest[book.identity] != snapshot_id:
                raise ValueError("superseded snapshot")
            indexes = {level.price: index for index, level in enumerate(book.asks)}
            for fill in fills:
                index = indexes.get(fill.price)
                if index is None or staged[snapshot_id][index] < fill.quantity:
                    raise ValueError("visible depth changed since quote")
                staged[snapshot_id][index] -= fill.quantity
        self.remaining = staged
