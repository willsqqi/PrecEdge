# PrecEdge research infrastructure rollout

## Working agreement

Each phase has a separate draft PR. Codex implements and tests that phase, writes
a short explanation of the resulting behavior and its limits, and links the PR
for review. The user reviews the diff and supplies comments. Codex addresses those
comments in the corresponding PR. The user has authorized continued iterations:
each PR gets complete tests, a successful CI checkpoint and updated documentation
before the next PR opens. Dependent changes use explicit stacked PR bases. Merging, deployment, and trading need a
separate user instruction; opening a PR does not perform any of them.

Existing uncommitted work stays in the primary checkout. Phase 1 is developed in
an isolated worktree based on `origin/main`. Data archives and cloud resources
are not part of this rollout.

| Phase | Deliverable | Review gate |
| --- | --- | --- |
| 1: Matching foundation | Reviewed contract schema, bounded candidate retrieval, settlement blockers, saved reports, offline benchmark, candidate-table importer | Inspect missing/conflicting-evidence behavior, reproducible benchmark and tests |
| 2: Paper trading | Recorded-book replay, two-leg depth-aware fills, explicit fee schedules, persistent SQLite decisions and positions, caps, cooldowns and kill switch | Verify cash accounting, no repeated fills on restart, stale-book refusal and settlement reconciliation |
| 3: Scanner and dashboard | Feed approved contracts and snapshots into the research pipeline; expose acceptance/refusal reasons, positions and experiment results | Run an end-to-end local recorded-data experiment and inspect dashboard evidence |

Phase 1 is additive. Existing discovery, hosted embedding options, manual review
and price scanners retain their current behavior. The new contract templates do
not inherit approval from a high similarity score or an AI recommendation.

## Review order and implemented checkpoints

The foundation PR has been merged. CI/build delivery is reviewed separately,
followed by phase 2a (book depth and fee primitives), phase 2b (persistent replay
and risk accounting), and phase 3 (saved scanner bridge and read-only dashboard).
Each dependent PR is based on the previous branch so its diff stays focused.
After merging a predecessor, retarget the next PR to main and rerun CI. Opening
all checkpoints does not merge them or begin live trading.

See [saved-book research](saved_book_research.md) for the complete local pipeline.
Real archived books still require actual as-of contract/binding reviews and fee
metadata; synthetic fixtures are validation inputs, not performance evidence.

## Architecture

```text
Existing saved approval_candidates (CSV or Parquet)
                    |
         prepare-contracts: unapproved JSONL
                    |
     Human reviews venue rules and fills canonical facts
                    |
       IDF retrieval -> similarity -> settlement gate
                    |
   Match report: verdicts, blockers, input fingerprints, coverage
                    |
        [Phase 2] recorded-book paper execution
                    |
        [Phase 3] scanner and dashboard integration
```

The research package is separate from the large existing sports scanner. Its
matcher and benchmark use only Python's standard library. Only the importer uses
the project's existing pandas/Parquet dependencies. No new dependency is added.

## Phase 1: run locally

With the project's Python environment activated:

```bash
python -m prediction_market.research.cli benchmark \
  --markets-per-venue 1000 --planted 100 --seed 20261009 \
  --output reports/research/matcher-benchmark.json

python -m prediction_market.research.cli prepare-contracts \
  --candidates data/cross_sports_arbitrage/processed/latest/approval_candidates.parquet \
  --output reports/research/contracts-to-review.jsonl

# After completing and saving the reviewed contracts:
python -m prediction_market.research.cli match \
  --contracts reports/research/contracts-reviewed.jsonl \
  --left-venue kalshi --right-venue polymarket \
  --output reports/research/matches.json

python -m pytest -q tests/test_research_foundation.py
```

An editable/package installation also exposes `precedge-research` with the same
arguments. The matching and benchmark commands need no API access, accounts,
model service or cloud deployment. The importer reads previously collected files;
it does not refresh the markets or recreate retired infrastructure. A missing
input, invalid contract, duplicate identity or empty selected venue is an error,
not a successful scan reporting no opportunities.

## Contract evidence

The importer carries over venue, market ID, title, status, raw rules text and
Polymarket token IDs. Other fields start blank on purpose. Complete these against
the source rulebooks rather than copying a single shared interpretation into both
venues without checking them:

| Field | Meaning |
| --- | --- |
| `event_key` | Reviewed canonical identity of the same event, including its date/scope |
| `yes_claim` | Reviewed identity of precisely the proposition that YES means |
| `contract_type` | `binary` proposition or `threshold` proposition |
| `resolution_source` | Canonical identifier of the source that decides the result |
| `resolution_time` | Contractual resolution instant, with an explicit timezone |
| `cancellation_rule` | Postponement/cancellation policy, including delay windows and payout |
| `draw_rule`, `overtime_rule`, `penalties_rule` | Specific edge-case policies; use `not_applicable` only after checking |
| `rules_text`, `rules_reference` | Venue's raw rule text and its source URL or retained evidence location |
| `reviewed_by`, `reviewed_at` | Reviewer and timezone-qualified review timestamp |
| `comparator`, `threshold`, `unit` | Required for threshold contracts; blank/null for binary propositions |
| `yes_token_id`, `no_token_id` | Distinct YES/NO tokens for Polymarket contracts |
| `status` | Must be active, open or trading in the reviewed input |

Venue identity is explicit: `polymarket` denotes the international venue used by
the existing collector, and `polymarket_us` is a separate identity. This PR does
not add a Polymarket US collector or declare either venue executable. Contracts
on opposite outcomes are not automatically inverted: review must align YES
claims explicitly. A Polymarket contract key includes its YES token so different
outcome orientations in the existing candidate tables are not conflated.

## Matching and verdicts

1. Tokenize the title and reviewed event/claim keys. Compute corpus IDF and build
   a right-venue inverted index, excluding tokens appearing in more than 8% of the
   combined corpus (with a two-document floor for tiny fixtures).
2. Use six rare tokens to retrieve candidates. Score at most ten per left
   contract. The report counts retrieved/scored pairs and queries truncated by
   the cap, so a low match count cannot conceal candidate truncation.
3. Combine IDF-weighted token overlap (45%), canonical event equality (30%) and
   canonical YES-claim equality (25%). These are heuristic weights, not learned
   probabilities. Default minimum score is 0.55.
4. Compare all required settlement facts independently of similarity. Missing
   evidence blocks equivalence even if both sides omit the same field. Conflicting
   thresholds, comparators, units, dates, rule sources or edge-case policies block
   equivalence. Naive/invalid timestamps are rejected; equivalent timezone
   representations of one instant agree.
5. Emit `identical`, `needs_review` or `rejected` with the exact blockers and both
   full input fingerprints and raw-rule hashes.

`identical` means the supplied reviewed facts pass these consistency checks. It
does not independently certify the review, infer financial edge, or approve an
order. The gate cannot detect a wrong canonical claim supplied by a reviewer or
an unrecorded rule change. Phase 2 must bind a decision to the exact fingerprints
and refuse stale or changed evidence. Status is the status in the input, not a
live freshness check.

## Benchmark and validation

The generated corpus has deterministic known equivalent pairs, disjoint filler
vocabularies and near matches with missing/conflicting settlement evidence. The
report records seed, config, platform, Python version, potential/scored pairs,
candidate reduction, recovery, false positives and elapsed time. A ground-truth
failure returns a nonzero exit code. CI verifies correctness without enforcing
host-dependent latency thresholds.

The generated corpus is deliberately controlled and easier than live market
language. Its recovery rate is not an estimate of real-world recall. Measuring
that requires a separately reviewed holdout corpus, including false matches,
ambiguous wording, missing sources, stale rules, different cancellation windows
and different sports' draw policies. No profitability claim follows from either
the synthetic benchmark or an `identical` match.

## Phase 2 delivery checkpoints

Phase 2 is split into recorded-book/fee execution primitives, followed by
persistent replay, risk and settlement accounting. See [paper trading](paper_trading.md)
for the implemented assumptions and validation. Each checkpoint remains a
separate PR; CI/build delivery is reviewed independently.

## Phase 2 constraints

- Use replay time, not wall time, for stale-book checks, caps and cooldowns.
- Use asks and visible levels for buys. Never fill an arbitrary size at the best
  price or treat absent depth as available liquidity. Consume depth within a
  snapshot so several pairs cannot reuse the same visible liquidity.
- Supply explicit per-market fee metadata. Unknown fees block simulation; no
  hidden zero-fee or current-fee assumption is permitted.
- Persist every decision, refusal and simulated fill. An immutable event ID,
  payload digest and config/contract fingerprints prevent repeated fills after
  restart or reinterpretation of one event under changed inputs.
- Reconcile risk limits from open positions. Closing/settling a position must
  release exposure; cumulative fills must not masquerade as current positions.
- Label two-leg fill assumptions explicitly. Simultaneous recorded-book fills
  cannot demonstrate real atomic cross-venue execution or eliminate leg risk.
- Report unresolved positions separately from realized P&L. Pay each leg using
  its own recorded settlement, including fractional/void payouts, then reconcile.

## Reference

Design inspiration: [anaborne/prediction-market-infra](https://github.com/anaborne/prediction-market-infra),
especially the separation of candidate similarity from settlement evidence and
its reproducible synthetic benchmarks. This is an independent implementation
for PrecEdge's existing data/review workflow; no source code was copied. The
reference's reported live outcomes and latency figures are not PrecEdge results.
