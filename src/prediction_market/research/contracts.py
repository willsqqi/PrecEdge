"""Explicit, reviewed settlement facts, independent of similarity or venue SDKs."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from typing import Any

VENUES = {"kalshi", "polymarket", "polymarket_us"}
UNKNOWN = {"", "unknown", "unclear", "unspecified", "tbd", "none", "null", "nan", "n/a", "na", "pending", "unverified"}


def known(value: str) -> bool:
    return value.strip().casefold() not in UNKNOWN


def canonical(value: str) -> str:
    return " ".join(value.split()).casefold()


def timestamp(value: str) -> datetime:
    """Reject naive dates instead of silently supplying a clock domain."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class Contract:
    venue: str
    market_id: str
    title: str
    # These are reviewed canonical keys, not values inferred from title similarity.
    event_key: str = ""
    yes_claim: str = ""
    contract_type: str = ""
    resolution_source: str = ""
    resolution_time: str = ""
    cancellation_rule: str = ""
    draw_rule: str = ""
    overtime_rule: str = ""
    penalties_rule: str = ""
    yes_token_id: str = ""
    no_token_id: str = ""
    rules_text: str = ""
    rules_reference: str = ""
    reviewed_by: str = ""
    reviewed_at: str = ""
    comparator: str = ""
    threshold: float | None = None
    unit: str = ""
    status: str = ""

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "threshold":
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                ):
                    raise ValueError("threshold must be a finite number or null")
            elif not isinstance(value, str):
                raise ValueError(f"{field.name} must be a string")
        if self.venue not in VENUES:
            raise ValueError(f"unsupported venue: {self.venue}")
        if not known(self.market_id) or self.market_id != self.market_id.strip():
            raise ValueError("market_id must be nonempty and have no surrounding whitespace")

    @property
    def key(self) -> str:
        suffix = f":{self.yes_token_id}" if self.venue.startswith("polymarket") and self.yes_token_id else ""
        return f"{self.venue}:{self.market_id}{suffix}"

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, allow_nan=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def rules_hash(self) -> str:
        return hashlib.sha256(self.rules_text.encode()).hexdigest()

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Contract:
        return cls(**row)


def settlement_blockers(left: Contract, right: Contract) -> tuple[str, ...]:
    """Fail closed: absence of a settlement fact is never evidence of agreement.

    Human review supplies canonical event/claim/rule keys. This verifies consistency
    of that evidence, not whether the human's interpretation of the rulebook is true.
    """
    blockers: list[str] = []
    if left.venue == right.venue:
        blockers.append("same_venue")
    required = (
        "title", "event_key", "yes_claim", "contract_type", "resolution_source",
        "resolution_time", "cancellation_rule", "draw_rule", "overtime_rule", "penalties_rule",
        "rules_text", "rules_reference", "reviewed_by", "reviewed_at", "status",
    )
    for contract in (left, right):
        for name in required:
            if not known(getattr(contract, name)):
                blockers.append(f"missing:{contract.key}:{name}")
            elif canonical(getattr(contract, name)) == "not_applicable" and name not in {
                "draw_rule", "overtime_rule", "penalties_rule",
            }:
                blockers.append(f"invalid_not_applicable:{contract.key}:{name}")
        if canonical(contract.status) not in {"active", "open", "trading"}:
            blockers.append(f"inactive:{contract.key}")
        if contract.contract_type not in {"binary", "threshold"}:
            blockers.append(f"unsupported_contract_type:{contract.key}")
        if contract.venue.startswith("polymarket"):
            for name in ("yes_token_id", "no_token_id"):
                if not known(getattr(contract, name)):
                    blockers.append(f"missing:{contract.key}:{name}")
            if contract.yes_token_id == contract.no_token_id and known(contract.yes_token_id):
                blockers.append(f"invalid_tokens:{contract.key}")
        for name in ("resolution_time", "reviewed_at"):
            if known(getattr(contract, name)):
                try:
                    timestamp(getattr(contract, name))
                except ValueError:
                    blockers.append(f"invalid_timestamp:{contract.key}:{name}")
        if contract.contract_type == "threshold":
            if contract.comparator not in {">", ">=", "<", "<=", "=="}:
                blockers.append(f"invalid_comparator:{contract.key}")
            if contract.threshold is None:
                blockers.append(f"missing:{contract.key}:threshold")
            if not known(contract.unit) or canonical(contract.unit) == "not_applicable":
                blockers.append(f"missing:{contract.key}:unit")
        elif contract.comparator or contract.threshold is not None or contract.unit:
            blockers.append(f"unexpected_threshold_fields:{contract.key}")
    for name in (
        "event_key", "yes_claim", "contract_type", "resolution_source",
        "cancellation_rule", "draw_rule", "overtime_rule", "penalties_rule",
    ):
        a, b = getattr(left, name), getattr(right, name)
        if known(a) and known(b) and canonical(a) != canonical(b):
            blockers.append(f"mismatch:{name}")
    try:
        if timestamp(left.resolution_time) != timestamp(right.resolution_time):
            blockers.append("mismatch:resolution_time")
    except ValueError:
        pass  # Already represented as missing/invalid evidence above.
    if left.contract_type == right.contract_type == "threshold":
        for name in ("comparator", "threshold", "unit"):
            a, b = getattr(left, name), getattr(right, name)
            equal = canonical(a) == canonical(b) if isinstance(a, str) else a == b
            if not equal:
                blockers.append(f"mismatch:{name}")
    return tuple(blockers)
