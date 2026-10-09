from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd
import pytest

from prediction_market.research.benchmark import run_benchmark, synthetic_contract, synthetic_corpus
from prediction_market.research.cli import load_contracts, main, prepare_contracts
from prediction_market.research.contracts import Contract, settlement_blockers
from prediction_market.research.matching import MatcherConfig, match_contracts, match_report


def pair() -> tuple[Contract, Contract]:
    return synthetic_contract("kalshi", "k-1", "orchid"), synthetic_contract("polymarket", "p-1", "orchid")


def test_equivalent_reviewed_facts_are_accepted_and_bind_input_fingerprints() -> None:
    left, right = pair()
    matches, stats = match_contracts([left], [right])
    assert matches[0].verdict == "identical"
    assert matches[0].left_fingerprint == left.fingerprint
    assert matches[0].right_rules_hash == right.rules_hash
    assert stats.identical_pairs == 1
    assert replace(left, rules_text="Corrected rule").fingerprint != left.fingerprint


@pytest.mark.parametrize("field", [
    "event_key", "yes_claim", "resolution_source", "cancellation_rule", "draw_rule",
    "overtime_rule", "penalties_rule", "resolution_time",
])
def test_conflicting_settlement_facts_block_equivalence(field: str) -> None:
    left, right = pair()
    value = "2030-01-03T00:00:00Z" if field == "resolution_time" else "different rule"
    assert f"mismatch:{field}" in settlement_blockers(left, replace(right, **{field: value}))


@pytest.mark.parametrize("field", [
    "event_key", "yes_claim", "contract_type", "resolution_source", "resolution_time",
    "cancellation_rule", "draw_rule", "overtime_rule", "penalties_rule", "rules_text",
    "rules_reference", "reviewed_by", "reviewed_at", "status", "yes_token_id", "no_token_id",
])
def test_missing_evidence_blocks_even_when_both_sides_are_missing(field: str) -> None:
    left, right = pair()
    left, right = replace(left, **{field: ""}), replace(right, **{field: ""})
    assert any(blocker.endswith(f":{field}") for blocker in settlement_blockers(left, right))


@pytest.mark.parametrize("value", ["2030-01-02", "2030-01-02T00:00:00", "bad timestamp"])
def test_invalid_or_naive_resolution_timestamp_is_blocked(value: str) -> None:
    left, right = pair()
    assert any(b.startswith("invalid_timestamp:") for b in settlement_blockers(left, replace(right, resolution_time=value)))


def test_timezone_equivalent_instants_are_equal() -> None:
    left, right = pair()
    assert not settlement_blockers(left, replace(right, resolution_time="2030-01-01T19:00:00-05:00"))


def test_threshold_units_comparators_and_zero_are_checked() -> None:
    left, right = (replace(c, contract_type="threshold", comparator=">=", threshold=0, unit="celsius") for c in pair())
    assert not settlement_blockers(left, right)
    for name, value in (("unit", "fahrenheit"), ("comparator", ">"), ("threshold", 1)):
        assert f"mismatch:{name}" in settlement_blockers(left, replace(right, **{name: value}))
    assert any(b.endswith(":threshold") for b in settlement_blockers(left, replace(right, threshold=None)))


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), True, "1"])
def test_invalid_thresholds_cannot_enter_contracts(threshold: object) -> None:
    with pytest.raises(ValueError, match="finite number"):
        replace(pair()[0], threshold=threshold)


def test_inactive_unknown_and_same_venue_contracts_are_blocked() -> None:
    left, right = pair()
    assert any(b.startswith("inactive:") for b in settlement_blockers(left, replace(right, status="closed")))
    assert any(b.startswith("missing:") for b in settlement_blockers(left, replace(right, resolution_source="UNKNOWN")))
    assert "same_venue" in settlement_blockers(left, replace(right, venue="kalshi"))
    assert any(b.startswith("invalid_tokens:") for b in settlement_blockers(left, replace(right, no_token_id=right.yes_token_id)))


def test_missing_source_is_reviewable_but_never_identical() -> None:
    left, right = pair()
    matches, _ = match_contracts([left], [replace(right, resolution_source="")])
    assert matches[0].verdict == "needs_review"
    assert matches[0].blockers


@pytest.mark.parametrize("placeholder", ["N/A", "pending", "unverified", "not_applicable"])
def test_matching_placeholder_sources_are_not_settlement_evidence(placeholder: str) -> None:
    left, right = (replace(c, resolution_source=placeholder) for c in pair())
    assert settlement_blockers(left, right)


def test_synthetic_ground_truth_is_recovered_without_false_positives() -> None:
    report = run_benchmark(300, 30, 17)
    assert report["correctness_passed"]
    assert report["recovered_pairs"] == 30
    assert report["false_positive_pairs"] == 0
    assert report["stats"]["scored_candidates"] < report["stats"]["potential_pairs"] / 100
    assert report["dataset"] == "synthetic"


def test_ordering_is_deterministic_and_duplicates_are_rejected() -> None:
    left, right, _ = synthetic_corpus(40, 10, 5)
    config = MatcherConfig()
    assert match_report(left, right, config) == match_report(left[::-1], right[::-1], config)
    with pytest.raises(ValueError, match="duplicate"):
        match_contracts(left + [left[0]], right)


def test_candidate_cap_reports_truncation_and_is_deterministic() -> None:
    left, right = pair()
    others = [replace(right, market_id=f"p-{i}", yes_token_id=f"yes-{i}") for i in range(6)]
    config = MatcherConfig(max_candidates=2, max_document_fraction=1)
    matches, stats = match_contracts([left], others, config)
    assert len(matches) == 2
    assert stats.retrieved_candidates == 6
    assert stats.truncated_queries == 1
    assert matches == match_contracts([left], others[::-1], config)[0]


@pytest.mark.parametrize("kwargs", [
    {"max_candidates": 0}, {"max_candidates": True}, {"seed_tokens": -1},
    {"min_score": float("nan")}, {"max_document_fraction": 0},
])
def test_invalid_matcher_configuration_is_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        MatcherConfig(**kwargs)


def test_existing_scanner_candidates_become_unapproved_templates(tmp_path: Path) -> None:
    left, right = pair()
    source = tmp_path / "approval_candidates.csv"
    rows = [dict(venue=c.venue, market_id=c.market_id, title=c.title, status=c.status,
                 rules_text=c.rules_text, yes_token_id=c.yes_token_id, no_token_id=c.no_token_id) for c in (left, right)]
    pd.DataFrame(rows + [rows[1]]).to_csv(source, index=False)
    contracts = prepare_contracts(source)
    assert len(contracts) == 2  # Identical repeated rows are deduplicated.
    assert all(not c.reviewed_by and not c.yes_claim for c in contracts)
    matches, _ = match_contracts([contracts[0]], [contracts[1]])
    assert all(m.verdict != "identical" for m in matches)
    output = tmp_path / "templates.jsonl"
    assert main(["prepare-contracts", "--candidates", str(source), "--output", str(output)]) == 0
    assert len(load_contracts(output)) == 2


def test_conflicting_templates_and_invalid_json_are_rejected(tmp_path: Path) -> None:
    left, _ = pair()
    source = tmp_path / "candidates.csv"
    rows = [asdict(left), asdict(replace(left, rules_text="different rules"))]
    pd.DataFrame(rows).to_csv(source, index=False)
    with pytest.raises(ValueError, match="conflicting"):
        prepare_contracts(source)
    path = tmp_path / "contracts.jsonl"
    path.write_text('{"venue": "kalshi"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="contracts.jsonl:1"):
        load_contracts(path)


def test_cli_matches_reviewed_contracts_and_records_excluded_venues(tmp_path: Path) -> None:
    left, right = pair()
    path = tmp_path / "contracts.jsonl"
    rows = (left, right, replace(right, venue="polymarket_us"))
    path.write_text("".join(json.dumps(asdict(c)) + "\n" for c in rows), encoding="utf-8")
    output = tmp_path / "matches.json"
    assert main(["match", "--contracts", str(path), "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["stats"]["identical_pairs"] == 1
    assert report["excluded_venues"] == ["polymarket_us"]
    assert main(["match", "--contracts", str(path), "--output", str(path)]) == 2
    assert len(load_contracts(path)) == 3


def test_cli_no_coverage_is_an_error_and_benchmark_is_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    output = tmp_path / "report.json"
    assert main(["match", "--contracts", str(path), "--output", str(output)]) == 2
    assert not output.exists()
    assert main(["benchmark", "--markets-per-venue", "50", "--planted", "10", "--output", str(output)]) == 0
    assert json.loads(output.read_text())["correctness_passed"]
