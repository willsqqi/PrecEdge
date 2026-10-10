# PrecEdge

**Under development.**

PrecEdge is a prediction market data platform and trading infrastructure for Polymarket and Kalshi. It brings market discovery, outcome matching, and orderbook analysis into a shared workflow for evaluating opportunities across exchanges.

The platform turns fragmented market data into comparable datasets, helping users identify equivalent contracts, assess price differences, and track opportunities over time.

## Core capabilities

- **Data pipelines:** Collect and normalize market, event, and orderbook data from both exchanges.
- **Market matching:** Discover related contracts and review outcome equivalence and settlement rules.
- **Price analysis:** Evaluate cross-market price gaps with liquidity, fee, and slippage considerations.
- **Monitoring:** Explore market coverage, review matched pairs, and track signals through a dashboard.
- **Historical analysis:** Preserve snapshots and signals to evaluate market behavior and inform trading strategy development.

## Offline research foundation

The first phase of the infrastructure upgrade adds reviewed contract evidence,
bounded IDF candidate retrieval, settlement-equivalence checks, and a reproducible
synthetic benchmark. Missing or conflicting rules block an identical verdict;
similarity alone never approves a contract pair.

```bash
python -m prediction_market.research.cli benchmark \
  --output reports/research/matcher-benchmark.json
```

The benchmark runs without accounts, network access or cloud services. Its
generated-data results are not evidence of trading profitability. Paper trading
and dashboard integration are planned as separate review phases.

See [workflow, commands, evidence requirements and limitations](docs/research_rollout.md).

## CI and build delivery

GitHub Actions runs the offline suite on Linux and macOS, checks the research
benchmark, and verifies built Python packages. Successful runs provide tested
wheel/source downloads, content checksums and validation evidence. Cloud
deployment is separate from this local build pipeline.

See [checks, downloads and local commands](docs/ci_cd.md).

Paper execution and persistent replay: [recorded-book research guide](docs/paper_trading.md). Run `precedge-research paper-demo --directory reports/paper-demo` for a fictional accounting experiment.
