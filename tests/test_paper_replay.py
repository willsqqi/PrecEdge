import json
from dataclasses import asdict, replace
from decimal import Decimal

import pytest

from prediction_market.research.benchmark import synthetic_contract
from prediction_market.research.books import Book, FeeSchedule, Level
from prediction_market.research.cli import main
from prediction_market.research.replay import ReplayConfig, ReplayLedger

NOW = "2029-01-01T00:00:20Z"


def inputs():
    contracts = [synthetic_contract("kalshi", "k", "orchid"), synthetic_contract("polymarket", "p", "orchid")]
    fees = [FeeSchedule(c.key, "per_contract", "0.01", "synthetic", "2028-01-01T00:00:00Z", "2030-01-01T00:00:00Z") for c in contracts]
    return contracts, fees


def populate(ledger):
    for index, (c, side) in enumerate(zip(inputs()[0], ("yes", "no"), strict=True)):
        book = Book(f"s{index}", c.key, c.fingerprint, side, "2029-01-01T00:00:00Z", (Level("0.3" if index == 0 else "0.5", 10),))
        ledger.process({"type": "book", "event_id": f"b{index}", "replay_at": NOW, "book": book.to_dict()})


def trade(event_id="t1", **changes):
    c, _ = inputs()
    return {"type": "trade", "event_id": event_id, "replay_at": NOW,
            "left_key": c[0].key, "right_key": c[1].key, "left_snapshot": "s0", "right_snapshot": "s1", "quantity": 3, **changes}


def settlement(index, yes="1", no="0", event_id=None):
    c = inputs()[0][index]
    return {"type": "settlement", "event_id": event_id or f"settle-{index}", "replay_at": "2030-01-02T00:00:10Z",
            "contract_key": c.key, "contract_fingerprint": c.fingerprint, "yes_payout": yes, "no_payout": no,
            "settled_at": "2030-01-02T00:00:00Z", "reference": "synthetic-resolution"}


def ledger(path, **config):
    contracts, fees = inputs()
    return ReplayLedger(path, contracts, fees, ReplayConfig(cooldown_seconds=0, **config))


def test_restart_returns_saved_decision_without_cash_or_depth_reuse(tmp_path):
    path = tmp_path / "paper.db"
    with ledger(path) as first:
        populate(first)
        original = first.process(trade())
        assert original["status"] == "accepted"
        assert first.report()["cash"] == "997.54"
    with ledger(path) as resumed:
        assert resumed.process(trade()) == {**original, "duplicate": True}
        assert resumed.report()["cash"] == "997.54"
        assert resumed._state()["remaining"]["s0"] == [7]
        assert len(resumed.report()["decisions"]) == 3
        with pytest.raises(ValueError, match="changed payload"):
            resumed.process(trade(quantity=1))
        result = resumed.process(trade("t2", quantity=8))
        assert result["status"] == "refused"  # Pair cap, not just depth.
        assert len(resumed.report()["positions"]) == 1


def test_partial_and_fractional_settlements_release_exposure_and_reconcile(tmp_path):
    with ledger(tmp_path / "paper.db") as replay:
        populate(replay)
        replay.process(trade())
        replay.process(settlement(0, "0.5", "0.5"))
        report = replay.report()
        assert report["cash"] == "999.04"
        assert report["unresolved_cost"] == "1.53"
        assert report["realized_pnl"] == "0"  # Pair is unresolved.
        assert report["open_positions"] == 1
        replay.process(settlement(1, "0.5", "0.5"))
        report = replay.report()
        assert report["cash"] == "1000.54"
        assert report["realized_pnl"] == "0.54"
        assert report["venue_exposure"] == {}
        assert report["open_positions"] == 0
        assert report["closed_positions"] == 1
        assert report["reconciliation_passed"]
        assert replay.process(settlement(1, "0.5", "0.5"))["duplicate"] is True
        with pytest.raises(ValueError, match="already has"):
            replay.process(settlement(1, "0.5", "0.5", "different-id"))


def test_divergent_venue_settlements_record_actual_loss(tmp_path):
    with ledger(tmp_path / "paper.db") as replay:
        populate(replay)
        replay.process(trade())
        replay.process(settlement(0, "0", "1"))
        replay.process(settlement(1, "1", "0"))
        report = replay.report()
        assert report["realized_pnl"] == "-2.46"
        assert report["divergent_settlements"] == ["t1"]
        assert report["reconciliation_passed"]


@pytest.mark.parametrize("config,reason", [
    ({"initial_cash": "1"}, "insufficient_cash"),
    ({"max_open_cost": "2"}, "max_open_cost"),
    ({"max_venue_cost": "1"}, "max_venue_cost"),
    ({"max_pair_quantity": 2}, "max_pair_quantity"),
])
def test_risk_refusals_leave_cash_and_depth_unchanged(tmp_path, config, reason):
    with ledger(tmp_path / "paper.db", **config) as replay:
        populate(replay)
        result = replay.process(trade())
        assert result["reason"] == reason
        assert replay._state()["remaining"]["s0"] == [10]
        assert replay.report()["cash"] == replay.config.initial_cash
        assert replay.report()["positions"] == []


def test_reconciled_open_caps_count_current_positions(tmp_path):
    with ledger(tmp_path / "paper.db", max_positions=1) as replay:
        populate(replay)
        replay.process(trade())
        assert replay.process(trade("t2", quantity=1))["reason"] == "max_open_positions"
        replay.process(settlement(0))
        replay.process(settlement(1))
        assert replay.report()["open_positions"] == 0
        assert replay.process(trade("t3", replay_at="2030-01-02T00:00:11Z"))["reason"] == "already_settled_contract"


def test_kill_switch_blocks_opens_but_allows_reconciliation(tmp_path):
    with ledger(tmp_path / "paper.db") as replay:
        populate(replay)
        replay.process(trade())
        kill = {"type": "kill_switch", "event_id": "kill", "replay_at": NOW, "enabled": True}
        replay.process(kill)
        assert replay.process(trade("t2"))["reason"] == "kill_switch"
        replay.process(settlement(0))
        replay.process(settlement(1))
        assert replay.report()["closed_positions"] == 1
        assert replay.report()["kill_switch"]


def test_cooldown_uses_replay_clock(tmp_path):
    contracts, fees = inputs()
    with ReplayLedger(tmp_path / "paper.db", contracts, fees, ReplayConfig(cooldown_seconds=10)) as replay:
        populate(replay)
        replay.process(trade())
        assert replay.process(trade("t2", quantity=1, replay_at="2029-01-01T00:00:29Z"))["reason"] == "pair_cooldown"
        assert replay.process(trade("t3", quantity=1, replay_at="2029-01-01T00:00:30Z"))["status"] == "accepted"


def test_changed_experiment_inputs_need_a_new_database(tmp_path):
    path = tmp_path / "paper.db"
    contracts, fees = inputs()
    ledger(path).close()
    for changed_contracts, changed_fees, config in [
        ([replace(contracts[0], rules_text="changed"), contracts[1]], fees, ReplayConfig(cooldown_seconds=0)),
        (contracts, [replace(fees[0], rate=Decimal("0")), fees[1]], ReplayConfig(cooldown_seconds=0)),
        (contracts, fees, ReplayConfig(cooldown_seconds=1)),
    ]:
        with pytest.raises(ValueError, match="inputs changed"):
            ReplayLedger(path, changed_contracts, changed_fees, config)


def test_malformed_event_rolls_back_and_out_of_order_event_is_not_silently_sorted(tmp_path):
    with ledger(tmp_path / "paper.db") as replay:
        populate(replay)
        before = replay.report()
        with pytest.raises(ValueError, match="backwards"):
            replay.process(trade("old", replay_at="2029-01-01T00:00:19Z"))
        with pytest.raises(KeyError):
            replay.process(trade("missing", left_key="absent"))
        assert replay.report() == before


@pytest.mark.parametrize("changes", [
    {"yes_payout": "0.7", "no_payout": "0.7"},
    {"reference": "unknown"}, {"contract_fingerprint": "f" * 64},
    {"settled_at": "2031-01-01T00:00:00Z"},
    {"settled_at": "2028-01-01T00:00:00Z"},
])
def test_invalid_settlement_cannot_credit_cash(tmp_path, changes):
    with ledger(tmp_path / "paper.db") as replay:
        populate(replay)
        replay.process(trade())
        before = replay.report()
        with pytest.raises(ValueError):
            replay.process({**settlement(0), **changes})
        assert replay.report() == before


def test_two_connections_do_not_double_fill_same_event(tmp_path):
    path = tmp_path / "paper.db"
    with ledger(path) as first, ledger(path) as second:
        populate(first)
        first.process(trade())
        assert second.process(trade())["duplicate"]
        assert second.report()["cash"] == "997.54"


def test_cli_replay_resume_and_output_guards(tmp_path):
    contracts, fees = inputs()
    cp, fp, ep, configp, db, out = [tmp_path / name for name in ("contracts.jsonl", "fees.json", "events.jsonl", "config.json", "paper.db", "report.json")]
    cp.write_text("".join(json.dumps(asdict(c)) + "\n" for c in contracts))
    fp.write_text(json.dumps([f.to_dict() for f in fees]))
    configp.write_text(json.dumps(asdict(ReplayConfig(cooldown_seconds=0))))
    with ledger(tmp_path / "source.db") as source:
        populate(source)
        events = [json.loads(row[0]) for row in source.db.execute("SELECT payload FROM events ORDER BY rowid")]
    ep.write_text("".join(json.dumps(e) + "\n" for e in events + [trade(), settlement(0), settlement(1)]))
    args = ["paper-replay", "--contracts", str(cp), "--fees", str(fp), "--events", str(ep), "--config", str(configp), "--database", str(db), "--output", str(out)]
    assert main(args) == 0
    before = out.read_bytes()
    assert json.loads(before)["realized_pnl"] == "0.54"
    assert main(args) == 0
    assert out.read_bytes() == before
    assert main(args[:-1] + [str(cp)]) == 2
    assert main(args[:-1] + [str(db)]) == 2


@pytest.mark.parametrize("changes", [{"max_positions": 0}, {"cooldown_seconds": True}, {"initial_cash": "NaN"}, {"minimum_edge": "-1"}, {"max_pair_quantity": 1.5}])
def test_invalid_risk_config(changes):
    with pytest.raises(ValueError):
        ReplayConfig(**changes)


def test_new_event_after_restart_cannot_reuse_consumed_snapshot_depth(tmp_path):
    path = tmp_path / "paper.db"
    with ledger(path, max_pair_quantity=30) as first:
        populate(first)
        first.process(trade())
    with ledger(path, max_pair_quantity=30) as resumed:
        assert resumed.process(trade("t2", quantity=8))["reason"] == "insufficient visible depth"
        assert resumed.report()["cash"] == "997.54"


def test_failure_after_planning_does_not_partially_commit_cash_or_depth(tmp_path, monkeypatch):
    import prediction_market.research.replay as module

    with ledger(tmp_path / "paper.db") as replay:
        populate(replay)
        before = replay.report()
        original_encode = module.encode

        def fail_saving_state(value):
            if isinstance(value, dict) and value.get("positions"):
                raise OSError("simulated storage failure")
            return original_encode(value)

        with monkeypatch.context() as context:
            context.setattr(module, "encode", fail_saving_state)
            with pytest.raises(OSError, match="storage failure"):
                replay.process(trade())
        assert replay.report() == before
        assert replay.process(trade())["status"] == "accepted"


def test_concurrent_connections_serialize_one_fill(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "paper.db"
    with ledger(path) as initial:
        populate(initial)

    def attempt(_):
        with ledger(path) as replay:
            return replay.process(trade())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert sum(bool(r.get("duplicate")) for r in results) == 1
    with ledger(path) as replay:
        assert len(replay.report()["positions"]) == 1
        assert replay.report()["cash"] == "997.54"


def test_fictional_demo_correctness_and_no_overwrite(tmp_path):
    from prediction_market.research.demo import run_demo

    target = tmp_path / "demo"
    report = run_demo(target)
    assert report["demo_correctness_passed"]
    assert report["data_kind"] == "fictional_synthetic_fixture"
    before = (target / "report.json").read_bytes()
    assert main(["paper-demo", "--directory", str(target)]) == 2
    assert (target / "report.json").read_bytes() == before
    assert main(["paper-demo", "--directory", str(tmp_path / "cli-demo")]) == 0
