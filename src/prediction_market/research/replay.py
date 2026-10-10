"""Transactional, restart-safe recorded-book paper execution and reconciliation."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from .books import Book, DepthPool, FeeSchedule, count, digest, money
from .contracts import Contract, known, timestamp
from .execution import Refusal, commit_plan, plan_pair


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class ReplayConfig:
    initial_cash: str = "1000"
    max_open_cost: str = "100"
    max_venue_cost: str = "75"
    max_pair_quantity: int = 10
    max_positions: int = 20
    cooldown_seconds: int = 60
    max_age_seconds: int = 30
    minimum_edge: str = "0"

    def __post_init__(self) -> None:
        for name in ("initial_cash", "max_open_cost", "max_venue_cost", "minimum_edge"):
            value = money(getattr(self, name))
            if value < 0 or (name != "minimum_edge" and value == 0):
                raise ValueError(f"invalid {name}")
            object.__setattr__(self, name, str(value))
        count(self.max_pair_quantity)
        count(self.max_positions)
        for name in ("cooldown_seconds", "max_age_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"invalid {name}")


def open_positions(state: dict) -> list[dict]:
    return [p for p in state["positions"] if any(leg["credit"] is None for leg in p["legs"])]


def exposures(state: dict) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for position in open_positions(state):
        for leg in position["legs"]:
            if leg["credit"] is None:
                venue = leg["contract_key"].split(":", 1)[0]
                totals[venue] = totals.get(venue, Decimal(0)) + money(leg["cost"]) + money(leg["fee"])
    return totals


class ReplayLedger:
    """One immutable experiment per database; changed evidence needs a new DB."""

    def __init__(self, path: Path, contracts: list[Contract], fees: list[FeeSchedule], config: ReplayConfig, provenance: dict | None = None):
        self.contracts = {c.key: c for c in contracts}
        self.fees = {f.contract_key: f for f in fees}
        self.config = config
        if not contracts or len(self.contracts) != len(contracts) or len(self.fees) != len(fees):
            raise ValueError("nonempty unique contracts and unique fee schedules required")
        if set(self.fees) - set(self.contracts):
            raise ValueError("fee schedule references an unknown contract")
        header = {"schema_version": 1, "engine_version": 1, "provenance": provenance or {}, "config": asdict(config),
                  "contracts": {key: asdict(c) for key, c in sorted(self.contracts.items())},
                  "fees": {key: f.to_dict() for key, f in sorted(self.fees.items())}}
        self.provenance = provenance or {}
        self.fingerprint = digest(header)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("CREATE TABLE IF NOT EXISTS experiment (id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL, header TEXT NOT NULL, state TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL)")
            existing = self.db.execute("SELECT fingerprint,header FROM experiment WHERE id=1").fetchone()
            if existing and digest(json.loads(existing[1])) != existing[0]:
                raise ValueError("experiment header fingerprint mismatch")
            if existing and existing[0] != self.fingerprint:
                # Phase 2 headers predate optional provenance; retain their original
                # fingerprint when the complete original inputs still agree.
                legacy = {key: value for key, value in header.items() if key not in {"provenance", "engine_version"}}
                if provenance or existing[0] != digest(legacy) or "engine_version" in json.loads(existing[1]):
                    raise ValueError("experiment inputs changed; use a new database")
                self.fingerprint = existing[0]
            if not existing:
                state = {"cash": config.initial_cash, "books": {}, "remaining": {},
                         "positions": [], "settlements": {}, "last_at": None,
                         "last_trade": {}, "kill_switch": False}
                self.db.execute("INSERT INTO experiment VALUES(1,?,?,?)", (self.fingerprint, encode(header), encode(state)))
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _state(self) -> dict:
        return json.loads(self.db.execute("SELECT state FROM experiment WHERE id=1").fetchone()[0])

    @staticmethod
    def _pool(state: dict) -> DepthPool:
        pool = DepthPool()
        for row in sorted(state["books"].values(), key=lambda row: timestamp(row["observed_at"])):
            pool.register(Book.from_dict(row))
        pool.remaining = {key: values.copy() for key, values in state["remaining"].items()}
        return pool

    def process(self, event: dict) -> dict:
        if not isinstance(event, dict) or not isinstance(event.get("event_id"), str) or not known(event["event_id"]):
            raise ValueError("event needs an immutable event_id")
        event_id = event["event_id"]
        payload_digest = digest(event)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.db.execute("SELECT digest,result FROM events WHERE event_id=?", (event_id,)).fetchone()
            if previous:
                if previous[0] != payload_digest:
                    raise ValueError("event ID reused with changed payload")
                self.db.commit()
                return {**json.loads(previous[1]), "duplicate": True}
            now = timestamp(event["replay_at"])
            state = self._state()
            if state["last_at"] and now < timestamp(state["last_at"]):
                raise ValueError("new event must not move replay time backwards")
            try:
                detail = self._apply(state, event)
                result = {"event_id": event_id, "status": "accepted", **detail}
            except Refusal as exc:
                result = {"event_id": event_id, "status": "refused", "reason": str(exc)}
            state["last_at"] = event["replay_at"]
            self.db.execute("UPDATE experiment SET state=? WHERE id=1", (encode(state),))
            self.db.execute("INSERT INTO events VALUES(?,?,?,?)", (event_id, payload_digest, encode(event), encode(result)))
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def _apply(self, state: dict, event: dict) -> dict:
        kind = event["type"]
        now = timestamp(event["replay_at"])
        if kind == "book":
            book = Book.from_dict(event["book"])
            contract = self.contracts.get(book.contract_key)
            if contract is None or contract.fingerprint != book.contract_fingerprint:
                raise ValueError("book references changed or unknown contract")
            if timestamp(book.observed_at) > now:
                raise ValueError("book observation is in the replay future")
            pool = self._pool(state)
            pool.register(book)
            state["books"][book.snapshot_id] = book.to_dict()
            state["remaining"] = pool.remaining
            return {"snapshot_id": book.snapshot_id}
        if kind == "kill_switch":
            if not isinstance(event["enabled"], bool):
                raise ValueError("kill switch enabled must be a boolean")
            state["kill_switch"] = event["enabled"]
            return {"kill_switch": event["enabled"]}
        if kind == "settlement":
            return self._settle(state, event)
        if kind != "trade":
            raise ValueError(f"unknown replay event type: {kind}")
        if state["kill_switch"]:
            raise Refusal("kill_switch")
        left, right = self.contracts[event["left_key"]], self.contracts[event["right_key"]]
        if left.key in state["settlements"] or right.key in state["settlements"]:
            raise Refusal("already_settled_contract")
        quantity = count(event["quantity"])
        pair_key = encode(sorted((left.key, right.key)))
        positions = open_positions(state)
        if len(positions) >= self.config.max_positions:
            raise Refusal("max_open_positions")
        pair_quantity = sum(p["quantity"] for p in positions if p["pair_key"] == pair_key)
        if pair_quantity + quantity > self.config.max_pair_quantity:
            raise Refusal("max_pair_quantity")
        last = state["last_trade"].get(pair_key)
        if last and (now - timestamp(last)).total_seconds() < self.config.cooldown_seconds:
            raise Refusal("pair_cooldown")
        pool = self._pool(state)
        try:
            books = [pool.books[event[key]] for key in ("left_snapshot", "right_snapshot")]
        except KeyError as exc:
            raise Refusal("missing_snapshot") from exc
        plan = plan_pair(pool, left, right, *books, self.fees.get(left.key), self.fees.get(right.key),
                         quantity, event["replay_at"], self.config.max_age_seconds, money(self.config.minimum_edge))
        totals = exposures(state)
        for leg in plan.legs:
            venue = leg.contract_key.split(":", 1)[0]
            totals[venue] = totals.get(venue, Decimal(0)) + leg.cost + leg.fee
        if sum(totals.values(), Decimal(0)) > money(self.config.max_open_cost):
            raise Refusal("max_open_cost")
        if any(total > money(self.config.max_venue_cost) for total in totals.values()):
            raise Refusal("max_venue_cost")
        if plan.debit > money(state["cash"]):
            raise Refusal("insufficient_cash")
        commit_plan(pool, plan)
        state["remaining"] = pool.remaining
        state["cash"] = str(money(state["cash"]) - plan.debit)
        position = {"position_id": event["event_id"], "pair_key": pair_key,
                    "opened_at": event["replay_at"], **plan.to_dict()}
        for leg in position["legs"]:
            leg["credit"] = None
            leg["settlement_event"] = None
        state["positions"].append(position)
        state["last_trade"][pair_key] = event["replay_at"]
        return {"position": position}

    def _settle(self, state: dict, event: dict) -> dict:
        key = event["contract_key"]
        contract = self.contracts[key]
        if key in state["settlements"]:
            raise ValueError("contract already has a settlement record")
        if event["contract_fingerprint"] != contract.fingerprint:
            raise ValueError("settlement contract fingerprint mismatch")
        if not isinstance(event["reference"], str) or not known(event["reference"]):
            raise ValueError("settlement requires an evidence reference")
        if timestamp(event["settled_at"]) > timestamp(event["replay_at"]):
            raise ValueError("settlement is in the replay future")
        payouts = {side: money(event[f"{side}_payout"]) for side in ("yes", "no")}
        if any(p < 0 or p > 1 for p in payouts.values()) or sum(payouts.values()) != 1:
            raise ValueError("binary YES and NO payouts must each be in [0,1] and sum to one")
        for position in state["positions"]:
            for leg in position["legs"]:
                if leg["contract_key"] == key and timestamp(event["settled_at"]) < timestamp(position["opened_at"]):
                    raise ValueError("settlement predates a recorded purchase")
        credit = Decimal(0)
        for position in state["positions"]:
            for leg in position["legs"]:
                if leg["contract_key"] == key and leg["credit"] is None:
                    payout = payouts[leg["outcome"]] * position["quantity"]
                    leg["credit"], leg["settlement_event"] = str(payout), event["event_id"]
                    credit += payout
        state["cash"] = str(money(state["cash"]) + credit)
        state["settlements"][key] = {"event_id": event["event_id"], "reference": event["reference"],
                                     "settled_at": event["settled_at"],
                                     **{side: str(value) for side, value in payouts.items()}}
        return {"contract_key": key, "credited": str(credit)}

    def report(self) -> dict:
        # One read snapshot prevents mixing a concurrent event with old cash/positions.
        self.db.execute("BEGIN")
        try:
            state = self._state()
            decisions = [json.loads(row[0]) for row in self.db.execute("SELECT result FROM events ORDER BY rowid")]
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        positions = state["positions"]
        closed = [p for p in positions if all(leg["credit"] is not None for leg in p["legs"])]
        spent = sum((money(p["debit"]) for p in positions), Decimal(0))
        credits = sum((money(leg["credit"]) for p in positions for leg in p["legs"] if leg["credit"] is not None), Decimal(0))
        expected_cash = money(self.config.initial_cash) - spent + credits
        realized = sum((sum((money(leg["credit"]) for leg in p["legs"]), Decimal(0)) - money(p["debit"]) for p in closed), Decimal(0))
        divergent = []
        for p in closed:
            records = [state["settlements"][leg["contract_key"]] for leg in p["legs"]]
            if records[0]["yes"] != records[1]["yes"]:
                divergent.append(p["position_id"])
        return {"schema_version": 1, "experiment_fingerprint": self.fingerprint,
                "provenance": self.provenance, "simulation_only": True, "execution_assumption": "simultaneous_full_two_leg_recorded_asks",
                "config": asdict(self.config), "cash": state["cash"], "initial_cash": self.config.initial_cash,
                "realized_pnl": str(realized), "open_positions": len(open_positions(state)),
                "closed_positions": len(closed), "unresolved_cost": str(sum(exposures(state).values(), Decimal(0))),
                "venue_exposure": {key: str(value) for key, value in exposures(state).items()},
                "reconciliation_passed": money(state["cash"]) == expected_cash,
                "divergent_settlements": divergent, "kill_switch": state["kill_switch"],
                "last_replay_at": state["last_at"], "positions": positions, "decisions": decisions}


def read_report(path: Path) -> dict:
    """Read an existing experiment without creating a database or replaying events."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        row = connection.execute("SELECT fingerprint,header FROM experiment WHERE id=1").fetchone()
        if row is None:
            raise ValueError("missing experiment header")
        header = json.loads(row[1])
        if digest(header) != row[0] or header.get("engine_version", 1) != 1:
            raise ValueError("unsupported or changed experiment header")
        ledger = ReplayLedger.__new__(ReplayLedger)
        ledger.db = connection
        ledger.fingerprint = row[0]
        ledger.config = ReplayConfig(**header["config"])
        ledger.provenance = header.get("provenance", {})
        return ledger.report()
    finally:
        connection.close()
