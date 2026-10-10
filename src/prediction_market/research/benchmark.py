"""Generated-corpus correctness and throughput benchmark, not market evidence."""
from __future__ import annotations

import platform
import random
import time
from dataclasses import asdict, replace

from .contracts import Contract
from .matching import MatcherConfig, match_contracts


def synthetic_contract(venue: str, market_id: str, tag: str) -> Contract:
    return Contract(
        venue=venue, market_id=market_id, title=f"Will {tag} win the fixture?",
        event_key=f"fixture-{tag}", yes_claim=f"{tag} wins", contract_type="binary",
        resolution_source="synthetic official result", resolution_time="2030-01-02T00:00:00Z",
        cancellation_rule="void at 0.5", draw_rule="no on a draw", overtime_rule="included",
        penalties_rule="included", yes_token_id=f"{market_id}-yes" if venue.startswith("polymarket") else "",
        no_token_id=f"{market_id}-no" if venue.startswith("polymarket") else "",
        rules_text=f"Synthetic fixture: {tag} wins, including overtime; draw is NO; cancellation pays 0.5.",
        rules_reference=f"synthetic:{tag}", reviewed_by="fixture-generator",
        reviewed_at="2026-01-01T00:00:00Z", status="open",
    )


def synthetic_corpus(size: int, planted: int, seed: int) -> tuple[list[Contract], list[Contract], set[tuple[str, str]]]:
    if isinstance(size, bool) or isinstance(planted, bool) or not 1 <= planted <= size:
        raise ValueError("require 1 <= planted <= markets per venue")
    rng = random.Random(seed)
    labels = rng.sample(range(1_000_000_000), size)
    left, right, truth = [], [], set()
    for i, label in enumerate(labels):
        left_tag = f"pair{label}" if i < planted else f"left{label}"
        right_tag = left_tag if i < planted else f"right{label}"
        a = synthetic_contract("kalshi", f"k-{i}", left_tag)
        b = synthetic_contract("polymarket", f"p-{i}", right_tag)
        left.append(a)
        right.append(b)
        if i < planted:
            truth.add((a.key, b.key))
    # Confusable candidates, half with conflicting rules and half with missing evidence.
    decoys = min(20, planted, size - planted)
    for i in range(decoys):
        source = right[i]
        right[planted + i] = replace(
            source, market_id=f"p-{planted+i}",
            yes_token_id=f"p-{planted+i}-yes", no_token_id=f"p-{planted+i}-no",
            cancellation_rule="wait fourteen days" if i % 2 == 0 else source.cancellation_rule,
            resolution_source="" if i % 2 else source.resolution_source,
        )
    rng.shuffle(left)
    rng.shuffle(right)
    return left, right, truth


def run_benchmark(size: int = 1000, planted: int = 100, seed: int = 20261009) -> dict:
    left, right, truth = synthetic_corpus(size, planted, seed)
    config = MatcherConfig()
    started = time.perf_counter()
    matches, stats = match_contracts(left, right, config)
    elapsed = time.perf_counter() - started
    actual = {(m.left_key, m.right_key) for m in matches if m.verdict == "identical"}
    recovered = len(actual & truth)
    false_positive = len(actual - truth)
    return {
        "schema_version": 1, "dataset": "synthetic", "seed": seed,
        "platform": platform.platform(), "python": platform.python_version(),
        "config": asdict(config), "stats": asdict(stats), "elapsed_seconds": elapsed,
        "planted_pairs": planted, "recovered_pairs": recovered,
        "recall": recovered / planted, "false_positive_pairs": false_positive,
        "candidate_reduction": stats.potential_pairs / max(1, stats.scored_candidates),
        "correctness_passed": actual == truth,
        "limitations": (
            "Generated vocabularies and reviewed rule fixtures are constructed ground truth. "
            "Recall and throughput do not estimate performance on live venues. "
            "Timing is one run on this host; no latency threshold is enforced."
        ),
    }
