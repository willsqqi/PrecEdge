# Recorded-book paper trading

This module simulates purchases from saved, fully refreshed ask books. It makes
no venue requests and submits no orders. Phase 2a provides the book, fee and
execution primitives; the next PR adds persistent replay, risk and accounting.

## Records and execution

`Book` binds an immutable snapshot ID to one reviewed contract fingerprint,
YES/NO outcome, timezone-qualified observation time and ascending unique ask
levels. Prices are decimal USD per whole contract, between 0 and 1. Quantities
are positive whole contracts; fractional Polymarket shares are not modelled.
An empty ask book is valid evidence of unavailable depth, never a fill.

A two-leg plan buys YES on one venue and NO on the other (either orientation),
with the same contract quantity. Each contract's reviewed settlement facts must
pass the existing consistency gate. Contract fingerprints must match the books,
review cannot be in the replay future, and the contractual resolution instant
must not have been reached. The model assumes each ordinary binary contract pays
USD 1 or 0; different currencies, collateral conversion and variable payout
contracts need separate adapters.

The replay clock determines book age and fee validity. Future observations,
stale or superseded books, unknown fees, incompatible evidence and insufficient
visible depth refuse the whole plan. Planning has no fill side effects.
`commit_plan` stages both legs and consumes their quoted levels atomically in a
shared in-memory depth pool. A repeated snapshot ID cannot replenish depth;
changed payload under that ID is an input error. A strictly newer full snapshot
resets the available depth for its contract/outcome. Deltas are not supported.

## Fee assumptions

Each market supplies an explicit `FeeSchedule` with model, rate, optional
minimum, rounding increment, evidence reference, review time and expiry. Even a
zero-fee schedule must be supplied and reviewed. There is no default venue fee.

- `per_contract`: sum `rate * quantity` across filled levels.
- `quadratic`: sum `rate * quantity * price * (1 - price)` across filled levels.
- Apply the schedule minimum to the aggregate leg charge, then round upward to
  the specified increment (default USD 0.01).

These are configurable simulation formulas, not a statement of either venue's
current fees. A schedule's reference must document why that formula, rounding
scope and validity interval are appropriate. Rebates, fee tiers, gas, funding,
settlement fees and currency conversion are not inferred. If applicable costs
cannot be represented, do not use the resulting edge as an executable estimate.

The plan's `conditional_edge` is quantity minus the two ask costs and supplied
entry fees. It must be strictly greater than a nonnegative minimum edge. This is
conditional on the reviewed equivalence and recorded execution assumption,
not realized P&L. Settlement will account for each leg's recorded payout
independently, including fractional/void outcomes.

## Validation and limits

Run `python -m pytest -q tests/test_paper_books.py`. Tests cover walking several
levels, exact decimal cost/fee rounding, clock boundaries, evidence changes,
unknown fees, snapshot collisions, exhausted depth and atomic refusal/commit.

The execution assumption is labelled
`simultaneous_full_two_leg_recorded_asks`. It excludes queue position, latency,
partial-leg execution, cancellation, adverse selection and live leg risk. A new
snapshot may contain liquidity already consumed in reality; simulated consumption
is local to the snapshot. Historical review times must genuinely precede the
experiment clock to avoid look-ahead. The next PR persists this state and exposes
refusals and unsettled positions in experiment reports.

## Persistent replay (phase 2b)

`precedge-research paper-demo --directory reports/paper-demo` creates a new,
fictional experiment: two books, one fill, two refusals and independent venue
settlements. It checks exact cash/P&L and replays the entire file a second time
without adding fills. Existing directories are refused to preserve experiments.
The expected USD 0.59 result is a fixture assertion, not trading performance.

Resume that experiment without changing its contracts, fees or config:

```bash
precedge-research paper-replay \
  --contracts reports/paper-demo/contracts.jsonl \
  --fees reports/paper-demo/fees.json \
  --config reports/paper-demo/config.json \
  --events reports/paper-demo/events.jsonl \
  --database reports/paper-demo/paper.sqlite \
  --output reports/paper-demo/resumed-report.json
```

Contracts use phase 1's reviewed JSONL schema. Fees are a JSON array of
`FeeSchedule` records. Config is an optional JSON object of `ReplayConfig` fields;
the defaults are USD 1,000 initial cash, USD 100 total unresolved cost, USD 75 per
venue, 10 open contracts per pair, 20 open positions, 60-second pair cooldown,
30-second book age and strictly positive conditional edge. These are bounded
research defaults, not a recommended live allocation. Caps include entry fees
and reflect current unsettled legs, rather than cumulative historical fills.

Events are JSONL objects with `event_id`, `type` and timezone-qualified
`replay_at`, processed in file order. Newly encountered events cannot move the
clock backwards. Supported payloads:

| Type | Additional fields |
| --- | --- |
| `book` | `book`: snapshot ID, contract key/fingerprint, YES/NO outcome, observation time and asks with decimal prices/whole quantities |
| `trade` | `left_key`, `right_key`, `left_snapshot`, `right_snapshot`, `quantity` |
| `kill_switch` | `enabled`: explicit boolean; blocks new opens while books and settlements remain processable |
| `settlement` | `contract_key`, `contract_fingerprint`, `yes_payout`, `no_payout`, `settled_at`, `reference` |

Settlements must supply both per-contract payouts in [0,1], summing to 1. This
includes ordinary 1/0 outcomes and 0.5/0.5 void outcomes. Each contract's own
record credits its held legs; one venue's outcome is never substituted for the
other. Settlement can precede the scheduled resolution for cancellation, but
cannot predate a held purchase or be in the replay future. A known settlement
blocks further opens on that contract. A second settlement under a different ID
is an error, not an amendment; corrections need a new experiment.

A pair remains open until both legs settle. The report separates cash, realized
P&L of fully settled pairs, remaining acquisition cost/exposure and unresolved
positions. It flags divergent venue YES payouts, including losses despite a
positive planned edge. It reconciles initial cash minus all debits plus actual
settlement credits. It does not invent an unrealized mark or count planned edge
as profit.

SQLite uses `BEGIN IMMEDIATE`, WAL and full synchronization. One transaction
commits the event payload/digest, decision, consumed depth, cash and positions.
Concurrent connections serialize; an exception rolls back the entire event.
Replaying an identical ID returns its saved result even after a restart. Reusing
an ID with changed payload is an error. Config, complete contract evidence and
fee schedules are fingerprinted in an immutable experiment header; changing any
of them requires a new database.

Valid but refused trades are saved with their reason. Malformed events abort the
command and roll back that event; earlier committed events remain available for
resume. Reports are written atomically after successful processing. Database and
report paths cannot overwrite inputs or one another. Keep the SQLite WAL/SHM
files with an active database; close all writers before copying it.

The ledger stores experiment state as JSON and retains every snapshot/event.
This favors inspectability for bounded local experiments; it is not a streaming
high-volume database. It has no position sales, financing, settlement fees,
real-time venue reconciliation or automatic fee refresh. Those assumptions must
be addressed separately before using a historical result to assess a live trade.

Run `python -m pytest -q tests/test_paper_replay.py` for restart/concurrency,
rollback, cap/cooldown, kill switch, fractional settlements, divergent outcomes,
CLI guards and the fictional end-to-end demo. CI also runs the demo from the
installed wheel with no runtime dependencies, and preserves its report with the
build artifact.
