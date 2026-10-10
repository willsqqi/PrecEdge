"""Small deterministic fictional experiment for package and accounting checks."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .benchmark import synthetic_contract
from .books import Book, FeeSchedule, Level
from .replay import ReplayConfig, ReplayLedger


def run_demo(directory: Path) -> dict:
    # Refuse to overwrite a previous experiment; resume it with paper-replay.
    directory.mkdir(parents=True, exist_ok=False)
    contracts = [synthetic_contract("kalshi", "demo-k", "orchid"),
                 synthetic_contract("polymarket", "demo-p", "orchid")]
    fees = [FeeSchedule(c.key, "per_contract", "0.01", "fictional-demo-fees-v1",
                        "2028-01-01T00:00:00Z", "2031-01-01T00:00:00Z") for c in contracts]
    config = ReplayConfig(cooldown_seconds=0)
    now = "2029-01-01T00:00:20Z"
    books = [Book("demo-b0", contracts[0].key, contracts[0].fingerprint, "yes", "2029-01-01T00:00:00Z",
                  (Level("0.30", 2), Level("0.40", 3))),
             Book("demo-b1", contracts[1].key, contracts[1].fingerprint, "no", "2029-01-01T00:00:00Z",
                  (Level("0.45", 4), Level("0.50", 1)))]
    events = [{"type": "book", "event_id": f"book-{i}", "replay_at": now, "book": b.to_dict()} for i, b in enumerate(books)]
    trade = {"type": "trade", "event_id": "fill-1", "replay_at": now,
             "left_key": contracts[0].key, "right_key": contracts[1].key,
             "left_snapshot": books[0].snapshot_id, "right_snapshot": books[1].snapshot_id, "quantity": 3}
    events += [trade, {**trade, "event_id": "depth-refusal", "quantity": 4},
               {"type": "kill_switch", "event_id": "pause", "replay_at": now, "enabled": True},
               {**trade, "event_id": "paused-refusal", "quantity": 1}]
    events += [{"type": "settlement", "event_id": f"settle-{i}", "replay_at": "2030-01-02T00:00:10Z",
                "contract_key": c.key, "contract_fingerprint": c.fingerprint, "yes_payout": "1", "no_payout": "0",
                "settled_at": "2030-01-02T00:00:00Z", "reference": "fictional-demo-settlement-v1"} for i, c in enumerate(contracts)]
    for name, value in (("fees.json", [f.to_dict() for f in fees]), ("config.json", asdict(config))):
        (directory / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    for name, rows in (("contracts.jsonl", [asdict(c) for c in contracts]), ("events.jsonl", events)):
        (directory / name).write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    with ReplayLedger(directory / "paper.sqlite", contracts, fees, config) as ledger:
        for event in events:
            ledger.process(event)
        before = ledger.report()
        for event in events:
            ledger.process(event)
        report = ledger.report()
    reasons = [d.get("reason") for d in report["decisions"] if d["status"] == "refused"]
    passed = (report == before and report["cash"] == "1000.59" and report["realized_pnl"] == "0.59"
              and report["open_positions"] == 0 and report["closed_positions"] == 1
              and report["reconciliation_passed"] and reasons == ["insufficient visible depth", "kill_switch"])
    report["demo_correctness_passed"] = passed
    report["data_kind"] = "fictional_synthetic_fixture"
    (directory / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report
