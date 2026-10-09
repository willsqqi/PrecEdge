"""Bounded IDF retrieval followed by an explicit settlement-evidence gate."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Iterable

from .contracts import Contract, canonical, known, settlement_blockers


def tokens(contract: Contract) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", canonical(
        f"{contract.title} {contract.event_key} {contract.yes_claim}"
    )))


@dataclass(frozen=True)
class MatcherConfig:
    max_candidates: int = 10
    seed_tokens: int = 6
    max_document_fraction: float = 0.08
    min_score: float = 0.55

    def __post_init__(self) -> None:
        for name in ("max_candidates", "seed_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_document_fraction", "min_score"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")


@dataclass(frozen=True)
class Match:
    left_key: str
    right_key: str
    left_fingerprint: str
    right_fingerprint: str
    score: float
    verdict: str
    blockers: tuple[str, ...]
    left_rules_hash: str
    right_rules_hash: str


@dataclass(frozen=True)
class MatchStats:
    left_count: int
    right_count: int
    potential_pairs: int
    retrieved_candidates: int
    scored_candidates: int
    truncated_queries: int
    indexed_tokens: int
    dropped_common_tokens: int
    identical_pairs: int


def match_contracts(
    left: Iterable[Contract], right: Iterable[Contract], config: MatcherConfig | None = None,
) -> tuple[list[Match], MatchStats]:
    config = config or MatcherConfig()
    left, right = sorted(left, key=lambda c: c.key), sorted(right, key=lambda c: c.key)
    keys = [contract.key for contract in (*left, *right)]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate contract identity")
    corpus = [tokens(c) for c in (*left, *right)]
    frequency = Counter(token for row in corpus for token in row)
    count = len(corpus)
    weights = {t: math.log((count + 1) / (n + 1)) + 1 for t, n in frequency.items()}
    # Small corpora retain singleton tokens even though 1/N exceeds the fraction.
    common = {t for t, n in frequency.items() if n > max(2, count * config.max_document_fraction)}
    index: dict[str, list[int]] = defaultdict(list)
    right_tokens = corpus[len(left):]
    for i, row in enumerate(right_tokens):
        for token in sorted(row - common):
            index[token].append(i)
    matches: list[Match] = []
    retrieved = truncated = 0
    for contract, row in zip(left, corpus[:len(left)], strict=True):
        seeds = sorted(row - common, key=lambda t: (-weights[t], t))[:config.seed_tokens]
        hits: dict[int, float] = defaultdict(float)
        for token in seeds:
            for i in index.get(token, ()):
                hits[i] += weights[token]
        retrieved += len(hits)
        truncated += len(hits) > config.max_candidates
        ranked = sorted(hits, key=lambda i: (-hits[i], right[i].key))[:config.max_candidates]
        for i in ranked:
            other = right[i]
            union = row | right_tokens[i]
            lexical = sum(weights[t] for t in sorted(row & right_tokens[i])) / sum(weights[t] for t in sorted(union))
            event = known(contract.event_key) and canonical(contract.event_key) == canonical(other.event_key)
            claim = known(contract.yes_claim) and canonical(contract.yes_claim) == canonical(other.yes_claim)
            score = 0.45 * lexical + 0.30 * event + 0.25 * claim
            blockers = settlement_blockers(contract, other)
            if score < config.min_score:
                blockers += ("low_similarity",)
            hard = any(b.startswith(("mismatch:", "inactive:", "unsupported_", "same_venue", "unexpected_")) for b in blockers)
            verdict = "identical" if not blockers else "rejected" if hard else "needs_review"
            matches.append(Match(
                contract.key, other.key, contract.fingerprint, other.fingerprint,
                score, verdict, blockers, contract.rules_hash, other.rules_hash,
            ))
    return matches, MatchStats(
        len(left), len(right), len(left) * len(right), retrieved, len(matches), truncated,
        len(index), len(common), sum(m.verdict == "identical" for m in matches),
    )


def match_report(left: list[Contract], right: list[Contract], config: MatcherConfig) -> dict:
    matches, stats = match_contracts(left, right, config)
    return {
        "schema_version": 1,
        "purpose": "offline research; identical is consistency of reviewed evidence, not a trading signal",
        "config": asdict(config), "stats": asdict(stats), "matches": [asdict(m) for m in matches],
    }
