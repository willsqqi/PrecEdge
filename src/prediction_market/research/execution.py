"""Depth-aware two-leg paper plans, bound to reviewed contract evidence."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .books import Book, DepthPool, FeeSchedule, Level, count, money
from .contracts import Contract, settlement_blockers, timestamp


class Refusal(ValueError):
    """A valid replay request refused by the simulation's evidence or price gates."""


@dataclass(frozen=True)
class LegFill:
    snapshot_id: str
    contract_key: str
    outcome: str
    fills: tuple[Level, ...]
    cost: Decimal
    fee: Decimal

    def to_dict(self) -> dict:
        return {"snapshot_id": self.snapshot_id, "contract_key": self.contract_key,
                "outcome": self.outcome, "fills": [f.to_dict() for f in self.fills],
                "cost": str(self.cost), "fee": str(self.fee)}


@dataclass(frozen=True)
class PairPlan:
    quantity: int
    legs: tuple[LegFill, LegFill]

    @property
    def debit(self) -> Decimal:
        return sum((leg.cost + leg.fee for leg in self.legs), Decimal(0))

    @property
    def conditional_edge(self) -> Decimal:
        # Conditional on equivalent binary outcomes and the recorded execution assumption.
        return self.quantity - self.debit

    def to_dict(self) -> dict:
        return {"quantity": self.quantity, "debit": str(self.debit),
                "conditional_edge": str(self.conditional_edge),
                "execution_assumption": "simultaneous_full_two_leg_recorded_asks",
                "legs": [leg.to_dict() for leg in self.legs]}


def plan_pair(
    pool: DepthPool, left: Contract, right: Contract,
    left_book: Book, right_book: Book,
    left_fee: FeeSchedule | None, right_fee: FeeSchedule | None,
    quantity: int, replay_at: str, max_age_seconds: int = 30,
    minimum_edge: Decimal = Decimal("0"),
) -> PairPlan:
    count(quantity)
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or max_age_seconds < 0:
        raise ValueError("max_age_seconds must be a nonnegative integer")
    minimum_edge = money(minimum_edge)
    if minimum_edge < 0:
        raise ValueError("minimum edge must be nonnegative")
    now = timestamp(replay_at)
    blockers = settlement_blockers(left, right)
    if blockers:
        raise Refusal("settlement_evidence:" + ",".join(blockers))
    if {left_book.outcome, right_book.outcome} != {"yes", "no"}:
        raise Refusal("requires_complementary_outcomes")
    legs = []
    for contract, book, fee in ((left, left_book, left_fee), (right, right_book, right_fee)):
        if contract.key != book.contract_key or contract.fingerprint != book.contract_fingerprint:
            raise Refusal("contract_fingerprint_mismatch")
        if timestamp(contract.reviewed_at) > now:
            raise Refusal("future_contract_review")
        if now >= timestamp(contract.resolution_time):
            raise Refusal("contract_resolution_reached")
        age = (now - timestamp(book.observed_at)).total_seconds()
        if age < 0 or age > max_age_seconds:
            raise Refusal("future_book" if age < 0 else "stale_book")
        if fee is None:
            raise Refusal("unknown_fee_schedule")
        if fee.contract_key != contract.key:
            raise Refusal("fee_contract_mismatch")
        if not timestamp(fee.reviewed_at) <= now < timestamp(fee.valid_until):
            raise Refusal("fee_outside_validity")
        if pool.books.get(book.snapshot_id) != book:
            raise Refusal("unregistered_snapshot")
        try:
            fills = pool.quote(book.snapshot_id, quantity)
        except ValueError as exc:
            raise Refusal(str(exc)) from exc
        cost = sum((level.price * level.quantity for level in fills), Decimal(0))
        legs.append(LegFill(book.snapshot_id, contract.key, book.outcome, fills, cost, fee.charge(fills)))
    plan = PairPlan(quantity, tuple(legs))
    if plan.conditional_edge <= minimum_edge:
        raise Refusal("insufficient_edge_after_fees")
    return plan


def commit_plan(pool: DepthPool, plan: PairPlan) -> None:
    pool.consume(tuple((leg.snapshot_id, leg.fills) for leg in plan.legs))
