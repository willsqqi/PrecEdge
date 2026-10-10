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
