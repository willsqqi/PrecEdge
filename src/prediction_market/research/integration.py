"""Strict bridge from saved scanner raw books to reviewed paper experiments."""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_FLOOR

from .books import Book, Level, count, digest, money
from .contracts import Contract, known, settlement_blockers, timestamp


def levels(raw: list, *, cents: bool = False, complement: bool = False) -> tuple[Level, ...]:
    if not isinstance(raw, list):
        raise ValueError("raw levels must be an explicit list")
    totals: dict[Decimal, int] = {}
    for row in raw:
        if isinstance(row, dict):
            price, size = money(row["price"]), money(row["size"])
        elif isinstance(row, list) and len(row) == 2:
            price, size = money(row[0]), money(row[1])
        else:
            raise ValueError("unsupported raw level shape")
        if cents:
            if price != price.to_integral_value():
                raise ValueError("legacy cent prices must be integers")
            price /= 100
        if not 0 <= price <= 1 or size < 0:
            raise ValueError("invalid raw price or size")
        if complement:
            price = 1 - price
        # Whole-contract execution conservatively discards each level's fractional tail.
        quantity = int(size.to_integral_value(rounding=ROUND_FLOOR))
        if quantity:
            totals[price] = totals.get(price, 0) + quantity
    return tuple(Level(price, size) for price, size in sorted(totals.items()))


def validate_bindings(contracts: list[Contract], bindings: list[dict]) -> dict[str, dict]:
    by_key = {c.key: c for c in contracts}
    if len(by_key) != len(contracts) or not bindings:
        raise ValueError("unique contracts and nonempty reviewed bindings required")
    result = {}
    for binding in bindings:
        for field in ("mapping_id", "kalshi_key", "polymarket_key", "reviewed_by", "reference", "reviewed_at"):
            if not isinstance(binding.get(field), str) or not known(binding[field]):
                raise ValueError(f"binding requires reviewed {field}")
        timestamp(binding["reviewed_at"])
        key = binding["mapping_id"]
        if key in result:
            raise ValueError("duplicate mapping binding")
        left, right = by_key[binding["kalshi_key"]], by_key[binding["polymarket_key"]]
        if left.venue != "kalshi" or right.venue != "polymarket":
            raise ValueError("saved scanner bridge supports Kalshi and international Polymarket only")
        blockers = settlement_blockers(left, right)
        if blockers:
            raise ValueError("unapproved binding:" + ",".join(blockers))
        result[key] = binding
    return result


def compile_scan(contracts: list[Contract], bindings: list[dict], rows: list[dict], quantity: int = 1) -> tuple[list[dict], dict]:
    """Books first within each recorded instant; deterministic pair/orientation order."""
    count(quantity)
    reviewed = validate_bindings(contracts, bindings)
    by_key = {c.key: c for c in contracts}
    groups: dict = {}
    skipped = 0
    for row in rows:
        binding = reviewed.get(str(row.get("mapping_id", "")))
        if binding is None:
            skipped += 1
            continue
        if str(row.get("error", "")).strip():
            raise ValueError("selected snapshot contains a collection error")
        venue = row["venue"]
        if venue not in {"kalshi", "polymarket"}:
            raise ValueError("unsupported scanner venue")
        contract = by_key[binding[f"{venue}_key"]]
        if str(row["market_id"]) != contract.market_id:
            raise ValueError("snapshot market ID differs from reviewed binding")
        observed = str(row["retrieved_at"])
        instant = timestamp(observed)
        if timestamp(binding["reviewed_at"]) > instant or timestamp(contract.reviewed_at) > instant:
            raise ValueError("review must precede recorded snapshot time")
        run_id = str(row["run_id"])
        if not known(run_id):
            raise ValueError("snapshot requires a run_id")
        raw = json.loads(row["raw_orderbook"]) if isinstance(row["raw_orderbook"], str) else row["raw_orderbook"]
        if not isinstance(raw, dict):
            raise ValueError("snapshot requires a retained raw orderbook object")
        if venue == "polymarket":
            asks = {}
            for side, token in (("yes", contract.yes_token_id), ("no", contract.no_token_id)):
                payload = raw[side]
                if str(payload.get("asset_id", "")) != token:
                    raise ValueError("raw token identity differs from reviewed contract")
                asks[side] = levels(payload["asks"])
        elif "orderbook_fp" in raw:
            payload = raw["orderbook_fp"]
            asks = {"yes": levels(payload["no_dollars"], complement=True),
                    "no": levels(payload["yes_dollars"], complement=True)}
        elif "orderbook" in raw:
            payload = raw["orderbook"]
            asks = {"yes": levels(payload["no"], cents=True, complement=True),
                    "no": levels(payload["yes"], cents=True, complement=True)}
        else:
            raise ValueError("unknown retained Kalshi orderbook schema")
        group = groups.setdefault(instant, {"books": {}, "mappings": set()})
        group["mappings"].add(binding["mapping_id"])
        for side in ("yes", "no"):
            snapshot_id = digest([run_id, contract.key, side, instant.isoformat()])
            book = Book(snapshot_id, contract.key, contract.fingerprint, side, instant.isoformat(), asks[side])
            previous = group["books"].get(snapshot_id)
            if previous is not None and previous != book:
                raise ValueError("same snapshot identity has conflicting raw depth")
            group["books"][snapshot_id] = book
    if not groups:
        raise ValueError("no saved snapshots selected by reviewed bindings")
    events = []
    latest = {}
    for instant, group in sorted(groups.items()):
        replay_at = instant.isoformat()
        for snapshot_id, book in sorted(group["books"].items()):
            latest[(book.contract_key, book.outcome)] = snapshot_id
            events.append({"type": "book", "event_id": "book:" + snapshot_id, "replay_at": replay_at, "book": book.to_dict()})
        for mapping_id in sorted(group["mappings"]):
            binding = reviewed[mapping_id]
            for left_side, right_side in (("yes", "no"), ("no", "yes")):
                left_id = latest.get((binding["kalshi_key"], left_side))
                right_id = latest.get((binding["polymarket_key"], right_side))
                proposal = {"type": "trade", "replay_at": replay_at, "left_key": binding["kalshi_key"],
                            "right_key": binding["polymarket_key"], "left_snapshot": left_id,
                            "right_snapshot": right_id, "quantity": quantity,
                            "mapping_id": mapping_id, "binding_fingerprint": digest(binding)}
                events.append({**proposal, "event_id": "proposal:" + digest(proposal)})
    return events, {"selected_book_records": sum(len(g["books"]) for g in groups.values()),
                    "proposals": sum(e["type"] == "trade" for e in events), "skipped_unbound_rows": skipped,
                    "bindings_fingerprint": digest(bindings), "source_rows_fingerprint": digest(rows),
                    "proposal_quantity": quantity, "proposal_order": "time_then_mapping_then_yes_no"}
