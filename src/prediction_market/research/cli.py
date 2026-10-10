"""Local-only research entry point; no venue calls and no hosted models."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from .benchmark import run_benchmark
from .contracts import Contract, VENUES
from .matching import MatcherConfig, match_report
from .books import FeeSchedule
from .replay import ReplayConfig, ReplayLedger, read_report
from .demo import run_demo
from .integration import compile_scan
import sqlite3


def load_contracts(path: Path) -> list[Contract]:
    contracts = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("each line must be a contract object")
            contracts.append(Contract.from_dict(row))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
    keys = [contract.key for contract in contracts]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate contract identity in input")
    return contracts


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_contracts(path: Path) -> list[Contract]:
    """Bridge existing approval_candidates to intentionally incomplete review templates."""
    import pandas as pd

    frame = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path, keep_default_na=False)
    missing = {"venue", "market_id", "title", "rules_text", "status"} - set(frame.columns)
    if missing:
        raise ValueError(f"candidate table missing columns: {', '.join(sorted(missing))}")
    contracts: dict[str, Contract] = {}
    for number, row in frame.iterrows():
        def text(name: str) -> str:
            value = row.get(name, "")
            return "" if pd.isna(value) else str(value)

        contract = Contract(
            venue=text("venue"), market_id=text("market_id"), title=text("title"),
            rules_text=text("rules_text"), status=text("status"),
            yes_token_id=text("yes_token_id"), no_token_id=text("no_token_id"),
        )
        previous = contracts.get(contract.key)
        if previous is not None and previous != contract:
            raise ValueError(f"conflicting candidate rows for {contract.key} at row {number}")
        contracts[contract.key] = contract
    return sorted(contracts.values(), key=lambda c: c.key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline contract research and reproducible matching benchmarks.")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-contracts", help="Create unapproved templates from saved candidate CSV/Parquet.")
    prepare.add_argument("--candidates", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    match = commands.add_parser("match", help="Retrieve candidates and check reviewed settlement facts.")
    match.add_argument("--contracts", type=Path, required=True)
    match.add_argument("--left-venue", choices=sorted(VENUES), default="kalshi")
    match.add_argument("--right-venue", choices=sorted(VENUES), default="polymarket")
    match.add_argument("--max-candidates", type=int, default=10)
    match.add_argument("--min-score", type=float, default=0.55)
    match.add_argument("--output", type=Path, required=True)
    bench = commands.add_parser("benchmark", help="Generated data; no credentials or network.")
    bench.add_argument("--markets-per-venue", type=int, default=1000)
    bench.add_argument("--planted", type=int, default=100)
    bench.add_argument("--seed", type=int, default=20261009)
    bench.add_argument("--output", type=Path, required=True)
    paper = commands.add_parser("paper-replay", help="Replay local JSONL events into a persistent paper ledger.")
    paper.add_argument("--contracts", type=Path, required=True)
    paper.add_argument("--fees", type=Path, required=True)
    paper.add_argument("--events", type=Path, required=True)
    paper.add_argument("--database", type=Path, required=True)
    paper.add_argument("--config", type=Path)
    paper.add_argument("--experiment-report", type=Path, help="Retain provenance when appending events to a saved-book scan.")
    paper.add_argument("--output", type=Path, required=True)
    demo = commands.add_parser("paper-demo", help="Create and verify a fictional experiment in a new directory.")
    demo.add_argument("--directory", type=Path, required=True)
    scan = commands.add_parser("paper-scan", help="Replay reviewed pairs from saved scanner raw orderbooks.")
    scan.add_argument("--contracts", type=Path, required=True)
    scan.add_argument("--bindings", type=Path, required=True)
    scan.add_argument("--snapshots", type=Path, required=True)
    scan.add_argument("--fees", type=Path, required=True)
    scan.add_argument("--config", type=Path)
    scan.add_argument("--quantity", type=int, default=1)
    scan.add_argument("--database", type=Path, required=True)
    scan.add_argument("--output", type=Path, required=True)
    inspect = commands.add_parser("paper-report", help="Export an existing ledger without replay or writes.")
    inspect.add_argument("--database", type=Path, required=True)
    inspect.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "paper-demo":
            report = run_demo(args.directory)
            print(json.dumps({k: report[k] for k in ("demo_correctness_passed", "data_kind", "cash", "realized_pnl", "reconciliation_passed")}))
            return 0 if report["demo_correctness_passed"] else 1
        if args.command == "paper-report":
            if args.output.resolve() == args.database.resolve():
                raise ValueError("output must not overwrite the database")
            report = read_report(args.database)
            write_atomic(args.output, json.dumps(report, indent=2, sort_keys=True) + "\n")
            return 0 if report["reconciliation_passed"] else 1
        if args.command == "paper-scan":
            import pandas as pd

            inputs = [args.contracts, args.bindings, args.snapshots, args.fees] + ([args.config] if args.config else [])
            if args.output.resolve() in {p.resolve() for p in inputs + [args.database]} or args.database.resolve() in {p.resolve() for p in inputs}:
                raise ValueError("database/output must not overwrite an input or each other")
            frame = pd.read_parquet(args.snapshots) if args.snapshots.suffix == ".parquet" else pd.read_csv(args.snapshots, dtype=str, keep_default_na=False)
            rows = frame.fillna("").to_dict(orient="records")
            contracts = load_contracts(args.contracts)
            events, provenance = compile_scan(contracts, json.loads(args.bindings.read_text()), rows, args.quantity)
            provenance["source_file_sha256"] = hashlib.sha256(args.snapshots.read_bytes()).hexdigest()
            fees = [FeeSchedule(**row) for row in json.loads(args.fees.read_text())]
            config = ReplayConfig(**json.loads(args.config.read_text())) if args.config else ReplayConfig()
            with ReplayLedger(args.database, contracts, fees, config, provenance) as ledger:
                for event in events:
                    ledger.process(event)
                report = ledger.report()
            write_atomic(args.output, json.dumps(report, indent=2, sort_keys=True) + "\n")
            print(json.dumps({"open_positions": report["open_positions"], "realized_pnl": report["realized_pnl"], "provenance": provenance}))
            return 0 if report["reconciliation_passed"] else 1
        source = getattr(args, "contracts", None) or getattr(args, "candidates", None)
        if source is not None and source.resolve() == args.output.resolve():
            raise ValueError("output must not overwrite the input")
        if args.command == "paper-replay":
            inputs = [args.contracts, args.fees, args.events] + ([args.config] if args.config else []) + ([args.experiment_report] if args.experiment_report else [])
            if args.output.resolve() in {p.resolve() for p in inputs + [args.database]} or args.database.resolve() in {p.resolve() for p in inputs}:
                raise ValueError("database/output must not overwrite an input or each other")
            config = ReplayConfig(**json.loads(args.config.read_text())) if args.config else ReplayConfig()
            fees = [FeeSchedule(**row) for row in json.loads(args.fees.read_text())]
            provenance = json.loads(args.experiment_report.read_text())["provenance"] if args.experiment_report else {}
            with ReplayLedger(args.database, load_contracts(args.contracts), fees, config, provenance) as ledger:
                for number, line in enumerate(args.events.read_text().splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        ledger.process(json.loads(line))
                    except (ValueError, TypeError, KeyError) as exc:
                        raise ValueError(f"{args.events}:{number}: {exc}") from exc
                report = ledger.report()
            write_atomic(args.output, json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
            print(json.dumps({k: v for k, v in report.items() if k not in {"positions", "decisions"}}, sort_keys=True))
            return 0 if report["reconciliation_passed"] else 1
        if args.command == "prepare-contracts":
            contracts = prepare_contracts(args.candidates)
            write_atomic(args.output, "".join(json.dumps(asdict(c), sort_keys=True) + "\n" for c in contracts))
            print(json.dumps({"templates": len(contracts), "approved": 0, "output": str(args.output)}))
            return 0
        if args.command == "match":
            if args.left_venue == args.right_venue:
                raise ValueError("choose two different venues")
            contracts = load_contracts(args.contracts)
            left = [c for c in contracts if c.venue == args.left_venue]
            right = [c for c in contracts if c.venue == args.right_venue]
            if not left or not right:
                raise ValueError("both selected venues must contain contracts")
            report = match_report(left, right, MatcherConfig(max_candidates=args.max_candidates, min_score=args.min_score))
            report["input_contracts"] = len(contracts)
            report["excluded_venues"] = sorted({c.venue for c in contracts} - {args.left_venue, args.right_venue})
            report["venue_pair"] = [args.left_venue, args.right_venue]
        else:
            report = run_benchmark(args.markets_per_venue, args.planted, args.seed)
        write_atomic(args.output, json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "matches"}, sort_keys=True))
        return 1 if report.get("correctness_passed") is False else 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        print(f"research: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
