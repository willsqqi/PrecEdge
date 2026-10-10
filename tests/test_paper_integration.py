import json
from dataclasses import asdict, replace
from decimal import Decimal

import pandas as pd
import pytest

from prediction_market.research.benchmark import synthetic_contract
from prediction_market.research.books import FeeSchedule
from prediction_market.research.cli import main
from prediction_market.research.dashboard import report_tables
from prediction_market.research.demo import run_demo
from prediction_market.research.integration import compile_scan, levels
from prediction_market.research.replay import ReplayConfig, ReplayLedger, read_report


def fixture():
    contracts = [synthetic_contract("kalshi", "k", "orchid"), synthetic_contract("polymarket", "p", "orchid")]
    binding = {"mapping_id": "mapping-1", "kalshi_key": contracts[0].key, "polymarket_key": contracts[1].key,
               "reviewed_by": "fixture", "reviewed_at": "2028-01-01T00:00:00Z", "reference": "fictional-binding"}
    common = {"run_id": "run-1", "retrieved_at": "2029-01-01T00:00:00Z", "mapping_id": "mapping-1", "error": ""}
    pm = {side: {"asset_id": token, "asks": [{"price": "0.45" if side == "no" else "0.75", "size": "10.5"}]}
          for side, token in (("yes", contracts[1].yes_token_id), ("no", contracts[1].no_token_id))}
    rows = [{**common, "venue": "kalshi", "market_id": "k", "raw_orderbook": json.dumps({"orderbook_fp": {"yes_dollars": [["0.20", "10"]], "no_dollars": [["0.6", "3"], ["0.7", "2"]]}})},
            {**common, "venue": "polymarket", "market_id": "p", "raw_orderbook": json.dumps(pm)}]
    fees = [FeeSchedule(c.key, "per_contract", "0.01", "fictional-fees", "2028-01-01T00:00:00Z", "2031-01-01T00:00:00Z") for c in contracts]
    return contracts, [binding], rows, fees


def test_saved_raw_books_to_two_leg_replay_and_unresolved_report(tmp_path):
    contracts, bindings, rows, fees = fixture()
    events, provenance = compile_scan(contracts, bindings, rows, quantity=3)
    assert provenance["selected_book_records"] == 4
    assert provenance["proposals"] == 2
    yes = next(e["book"] for e in events if e["type"] == "book" and e["book"]["contract_key"] == contracts[0].key and e["book"]["outcome"] == "yes")
    assert yes["asks"] == [{"price": "0.3", "quantity": 2}, {"price": "0.4", "quantity": 3}]
    path = tmp_path / "paper.db"
    with ReplayLedger(path, contracts, fees, ReplayConfig(cooldown_seconds=0), provenance) as ledger:
        results = [ledger.process(e) for e in events]
        assert results[-2]["status"] == "accepted"
        assert results[-1]["reason"] == "insufficient_edge_after_fees"
        report = ledger.report()
        assert report["cash"] == "997.59"
        assert report["realized_pnl"] == "0"
        assert report["unresolved_cost"] == "2.41"
        assert report["open_positions"] == 1
        for event in events:
            assert ledger.process(event)["duplicate"]
        assert ledger.report() == report
    assert read_report(path) == report
    with pytest.raises(ValueError, match="inputs changed"):
        ReplayLedger(path, contracts, fees, ReplayConfig(cooldown_seconds=0), {**provenance, "proposal_quantity": 4})


def test_legacy_cent_schema_uses_one_cent_as_one_cent():
    contracts, bindings, rows, _ = fixture()
    rows[0]["raw_orderbook"] = json.dumps({"orderbook": {"yes": [[1, 4]], "no": [[70, 5]]}})
    events, _ = compile_scan(contracts, bindings, rows)
    no = next(e["book"] for e in events if e["type"] == "book" and e["book"]["contract_key"] == contracts[0].key and e["book"]["outcome"] == "no")
    assert no["asks"][0]["price"] == "0.99"


def test_fractional_depth_is_floored_per_level_and_sorted_aggregated():
    result = levels([["0.5", "1.9"], ["0.3", "0.9"], ["0.5", "2.1"]])
    assert result[0].price == Decimal("0.5")
    assert result[0].quantity == 3


@pytest.mark.parametrize("change", [
    {"raw_orderbook": "{}", "yes_ask": "0.3", "yes_ask_depth": "100"},
    {"market_id": "other"}, {"error": "request_failed"},
    {"raw_orderbook": '{"orderbook_fp":{"yes_dollars":[]}}'},
    {"retrieved_at": "2029-01-01"},
])
def test_bad_selected_raw_records_fail_closed(change):
    contracts, bindings, rows, _ = fixture()
    rows[0].update(change)
    with pytest.raises((ValueError, KeyError)):
        compile_scan(contracts, bindings, rows)


def test_raw_polymarket_tokens_must_match_reviewed_orientation():
    contracts, bindings, rows, _ = fixture()
    raw = json.loads(rows[1]["raw_orderbook"])
    raw["yes"]["asset_id"] = contracts[1].no_token_id
    rows[1]["raw_orderbook"] = raw
    with pytest.raises(ValueError, match="token identity"):
        compile_scan(contracts, bindings, rows)


@pytest.mark.parametrize("change", [{"reviewed_at": "2030-01-01T00:00:00Z"}, {"reviewed_by": "unknown"}, {"reference": ""}])
def test_bindings_require_as_of_review_evidence(change):
    contracts, bindings, rows, _ = fixture()
    bindings[0].update(change)
    with pytest.raises(ValueError):
        compile_scan(contracts, bindings, rows)


def test_unreviewed_contracts_and_empty_selection_cannot_be_success():
    contracts, bindings, rows, _ = fixture()
    with pytest.raises(ValueError, match="unapproved"):
        compile_scan([replace(contracts[0], reviewed_by=""), contracts[1]], bindings, rows)
    rows[0]["mapping_id"] = "unbound"
    rows[1]["mapping_id"] = "unbound"
    with pytest.raises(ValueError, match="no saved snapshots"):
        compile_scan(contracts, bindings, rows)


def test_duplicate_shared_book_does_not_create_more_liquidity():
    contracts, bindings, rows, _ = fixture()
    baseline, _ = compile_scan(contracts, bindings, rows)
    duplicated, _ = compile_scan(contracts, bindings, rows + [rows[0].copy()])
    assert duplicated == baseline
    changed = rows[0].copy()
    changed["raw_orderbook"] = json.dumps({"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.7", "999"]]}})
    with pytest.raises(ValueError, match="conflicting raw depth"):
        compile_scan(contracts, bindings, rows + [changed])


@pytest.mark.parametrize("suffix", [".csv", ".parquet"])
def test_saved_table_cli_is_repeatable_with_provenance_and_readonly_export(tmp_path, suffix):
    contracts, bindings, rows, fees = fixture()
    cp, bp, fp, snap, db, out = [tmp_path / name for name in ("contracts.jsonl", "bindings.json", "fees.json", "snapshots" + suffix, "paper.db", "report.json")]
    cp.write_text("".join(json.dumps(asdict(c)) + "\n" for c in contracts))
    bp.write_text(json.dumps(bindings))
    fp.write_text(json.dumps([f.to_dict() for f in fees]))
    frame = pd.DataFrame(rows)
    if suffix == ".csv":
        frame.to_csv(snap, index=False)
    else:
        frame.to_parquet(snap, index=False)
    args = ["paper-scan", "--contracts", str(cp), "--bindings", str(bp), "--fees", str(fp), "--snapshots", str(snap), "--database", str(db), "--output", str(out)]
    assert main(args) == 0
    before = out.read_bytes()
    assert main(args) == 0
    assert out.read_bytes() == before
    exported = tmp_path / "export.json"
    assert main(["paper-report", "--database", str(db), "--output", str(exported)]) == 0
    assert exported.read_bytes() == before
    assert main(args[:-1] + [str(db)]) == 2
    assert main(["paper-report", "--database", str(tmp_path / "missing.db"), "--output", str(exported)]) == 2
    assert not (tmp_path / "missing.db").exists()


def test_dashboard_tables_preserve_decimal_values_and_separate_leg_settlements(tmp_path):
    report = run_demo(tmp_path / "demo")
    positions, decisions = report_tables(report)
    assert len(positions) == 2
    assert positions[0]["entry_cost"] == "1.00"
    assert positions[0]["pair_status"] == "settled"
    assert positions[0]["settlement_credit"] == "3"
    assert {d["reason"] for d in decisions if d["status"] == "refused"} == {"insufficient visible depth", "kill_switch"}
    with pytest.raises(ValueError, match="unsupported"):
        report_tables({**report, "simulation_only": False})


def test_phase_two_header_remains_readable_and_resumable(tmp_path):
    from prediction_market.research.books import digest

    contracts, _, _, fees = fixture()
    path = tmp_path / "legacy.db"
    config = ReplayConfig()
    with ReplayLedger(path, contracts, fees, config) as ledger:
        header = json.loads(ledger.db.execute("SELECT header FROM experiment").fetchone()[0])
        del header["provenance"]
        del header["engine_version"]
        old_fingerprint = digest(header)
        ledger.db.execute("UPDATE experiment SET header=?,fingerprint=?", (json.dumps(header), old_fingerprint))
    assert read_report(path)["experiment_fingerprint"] == old_fingerprint
    with ReplayLedger(path, contracts, fees, config) as resumed:
        assert resumed.fingerprint == old_fingerprint
        assert resumed.report()["provenance"] == {}


def test_scan_ledger_can_append_actual_settlement_events_via_cli(tmp_path):
    contracts, bindings, rows, fees = fixture()
    events, provenance = compile_scan(contracts, bindings, rows, quantity=3)
    db = tmp_path / "paper.db"
    config = ReplayConfig(cooldown_seconds=0)
    with ReplayLedger(db, contracts, fees, config, provenance) as ledger:
        for event in events:
            ledger.process(event)
        report = ledger.report()
    cp, fp, cfg, rp, ep, output = [tmp_path / name for name in ("contracts.jsonl", "fees.json", "config.json", "scan.json", "settlements.jsonl", "settled.json")]
    cp.write_text("".join(json.dumps(asdict(c)) + "\n" for c in contracts))
    fp.write_text(json.dumps([f.to_dict() for f in fees]))
    cfg.write_text(json.dumps(asdict(config)))
    rp.write_text(json.dumps(report))
    settlements = [{"type": "settlement", "event_id": f"s{i}", "replay_at": "2030-01-02T00:00:00Z",
                    "contract_key": c.key, "contract_fingerprint": c.fingerprint, "yes_payout": "0.5", "no_payout": "0.5",
                    "settled_at": "2030-01-02T00:00:00Z", "reference": "fictional-void"} for i, c in enumerate(contracts)]
    ep.write_text("".join(json.dumps(e) + "\n" for e in settlements))
    args = ["paper-replay", "--contracts", str(cp), "--fees", str(fp), "--config", str(cfg), "--experiment-report", str(rp),
            "--events", str(ep), "--database", str(db), "--output", str(output)]
    assert main(args) == 0
    result = json.loads(output.read_text())
    assert result["open_positions"] == 0
    assert result["realized_pnl"] == "0.59"
    assert result["provenance"] == provenance
    assert main(args) == 0


def test_two_reviewed_mapping_ids_share_one_recorded_depth_pool(tmp_path):
    contracts, bindings, rows, fees = fixture()
    second_binding = {**bindings[0], "mapping_id": "mapping-2"}
    second_rows = [{**row, "mapping_id": "mapping-2"} for row in rows]
    events, provenance = compile_scan(contracts, bindings + [second_binding], rows + second_rows, quantity=3)
    assert provenance["selected_book_records"] == 4  # Shared physical snapshots.
    with ReplayLedger(tmp_path / "paper.db", contracts, fees, ReplayConfig(cooldown_seconds=0), provenance) as ledger:
        results = [ledger.process(event) for event in events]
        assert len(ledger.report()["positions"]) == 1
        assert any(result.get("reason") == "insufficient visible depth" for result in results)


def test_empty_or_malformed_levels_cannot_create_phantom_liquidity():
    assert levels([]) == ()
    for invalid in ([["0.3", "-1"]], [["NaN", "1"]], [["1.2", "1"]], [["0.3"]]):
        with pytest.raises(ValueError):
            levels(invalid)
