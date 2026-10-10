# Saved scanner books and paper research

Phase 3 connects existing local scanner snapshots to the reviewed-contract
pipeline and exposes paper experiments in a read-only dashboard. All inputs are
saved files. The commands do not collect fresh market data, use cloud services,
refresh fees or place orders.

## Review the binding first

Complete the phase 1 contract JSONL, checking both venues' rules independently.
Provide a JSON array of explicit bindings from the scanner's `mapping_id` to the
reviewed contract identities:

```json
[
  {
    "mapping_id": "your-existing-mapping-id",
    "kalshi_key": "kalshi:YOUR-TICKER",
    "polymarket_key": "polymarket:YOUR-MARKET-ID:YOUR-YES-TOKEN",
    "reviewed_by": "reviewer-name",
    "reviewed_at": "2026-10-09T10:00:00Z",
    "reference": "retained-mapping-review"
  }
]
```

The bridge validates the complete settlement gate, market IDs and Polymarket
YES/NO token identities against retained raw books. Both the contract review and
binding review must precede the recorded snapshot. A legacy approval or a high
similarity score does not supply the new reviewed settlement facts. Do not backdate
a review to make historical data pass: use an appropriate as-of evidence record,
or begin collecting after the actual review. Canonical reviews still cannot
detect an unrecorded change to venue rules.

## Replay saved snapshots

The scanner already retains `raw_orderbook` in `orderbook_snapshots.csv/parquet`.
Supply that table, the reviewed bindings/contracts, and explicit fee schedules:

```bash
precedge-research paper-scan \
  --contracts reports/research/contracts-reviewed.jsonl \
  --bindings reports/research/bindings-reviewed.json \
  --fees reports/research/fees-reviewed.json \
  --snapshots data/cross_sports_arbitrage/processed/orderbook_snapshots.parquet \
  --quantity 1 \
  --database reports/research/experiment.sqlite \
  --output reports/research/experiment.json
```

`--config` accepts the risk config described in [paper trading](paper_trading.md).
Without it the replay defaults apply, including a 60-second cooldown. Tables may
also be CSV; identifiers are read as strings to preserve long token IDs.

Selected rows require `mapping_id`, `venue`, `market_id`, `run_id`, timezone-qualified
`retrieved_at`, `raw_orderbook` and a blank collection error. Unbound rows are
excluded and counted. An empty selected set is an error. Selected malformed or
failed collections abort compilation rather than disappearing from the scan.

The converter uses retained Polymarket token ask levels and derives Kalshi asks
from the complementary bid side. It supports explicit fixed-point dollar fields
and retained legacy integer-cent fields; legacy price 1 means USD 0.01, not USD 1.
Missing sides/unknown schemas are errors. An explicit empty side provides no
liquidity. It never treats total `yes_ask_depth`/`no_ask_depth` as available at the
best ask. Fractional tails are discarded at each raw level, duplicate prices are
aggregated, and levels are sorted by price. This is conservative whole-contract
depth, not full fractional-share execution.

The clock is the scanner's client-recorded `retrieved_at`, not a claim of exchange
synchronization. Within each recorded instant the compiler registers books first,
then considers reviewed mappings in sorted order, YES/NO orientation before NO/YES.
Available older opposite-venue books remain subject to the replay stale-book gate;
missing books produce saved refusals. All proposals share consumed depth and risk
limits. This deterministic order is an experiment policy, not an optimization of
portfolio allocation; cooldown/caps can refuse the second orientation.

The experiment header binds source rows/file checksum, full reviewed bindings,
proposal size and ordering policy. An identical rerun returns saved decisions
without additional fills. Changing the dataset, review, fee schedule, quantity or
config requires a new database. Snapshots with the same immutable identity and
conflicting depth are refused. This command targets a complete bounded saved
experiment, not an incrementally changing live dataset.

Saved scanner books alone have no settlement evidence. Their scan therefore
reports open positions and zero realized P&L until actual recorded settlements
are supplied. Append settlement JSONL events using the same contracts, fees,
config and database, retaining the scan's provenance:

```bash
precedge-research paper-replay \
  --contracts reports/research/contracts-reviewed.jsonl \
  --fees reports/research/fees-reviewed.json \
  --events reports/research/settlements.jsonl \
  --experiment-report reports/research/experiment.json \
  --database reports/research/experiment.sqlite \
  --output reports/research/settled-experiment.json
```

If the scan used `--config`, pass the same config here too. New events must not
move replay time backwards. Settlement schema/independent leg payouts are
specified in the paper-trading guide. Phase 2 databases remain resumable with
their original experiment fingerprints.

## Review results

Export an existing ledger without replaying or creating a database:

```bash
precedge-research paper-report \
  --database reports/research/experiment.sqlite \
  --output reports/research/refreshed-report.json
```

The existing source checkout monitor has a **Paper Research** tab and a **Local
paper database** input. This tab does not execute events or write risk settings.
It reads a consistent SQLite snapshot, shows cash/realized P&L/unresolved cost,
leg settlement credits and all decision/refusal rows, and flags failed cash
reconciliation or divergent settlements.

A standalone view is also included in the wheel:

```bash
python -m pip install ".[dashboard]"
PRECEDGE_PAPER_DATABASE=reports/research/experiment.sqlite precedge-paper-dashboard
```

The path can be changed in the view. Missing databases show guidance without
creating files. The dashboard needs the optional Streamlit extra; book matching,
paper replay and report export need only the standard library. The saved-table
importer uses the existing pandas/Parquet dependencies.

## Validation

Tests cover saved CSV and Parquet replay, precise depth/cost, token and market
identity mismatches, missing raw levels, as-of review, duplicated shared liquidity,
restart-safe provenance, actual appended settlements and legacy header compatibility.
Streamlit's AppTest renders the standalone view and checks metrics, refusal rows,
missing-file handling and unchanged database contents. A separate required CI job
installs the dashboard extra and runs these UI tests before build delivery.

The end-to-end source adapter fixture is fictional. No validated live performance
result or independently reviewed real-world contract corpus is introduced by this
PR. Existing archived tables can be inspected, but paper fills require the new
reviewed evidence and fee metadata.

Format references: [Kalshi orderbook schema and bid/ask complement](https://docs.kalshi.com/api-reference/market/get-market-orderbook),
[Polymarket's retained SDK orderbook parser](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/utilities.py).
These references define the supported saved formats; the bridge does not assert
compatibility with every current live API response.
