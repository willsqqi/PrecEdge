# Local Market Top-1 Reranker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and evaluate a fail-closed, fully local Polymarket-to-Kalshi market reranker whose accepted top-1 output is resolution-equivalent rather than merely topically similar.

**Architecture:** Reconstruct unique market facts and per-event candidate pools from the current local CSV, encode venue text with a locally executed sentence-transformer, and rerank with explicit literal-fact compatibility. Keep candidate ranking separate from the acceptance decision so weak or contradictory nearest neighbors become `ambiguous` or `no_match`.

**Tech Stack:** Python 3.13, pandas, NumPy, RapidFuzz, sentence-transformers, pytest, CSV/Parquet/JSON local artifacts.

## Global Constraints

- Do not call GCP, Vertex AI, Gemini, or any hosted inference API.
- Do not overwrite `manual_review/approved_market_pairs/current.csv` or other approval history.
- Use only venue-provided text and identifiers as matching evidence.
- Do not use the current `market_type` or bulk approval status as equivalence truth.
- Emit an accepted top-1 only when there is no critical contradiction and score/margin gates pass.
- Preserve `ambiguous` and `no_match` rows with diagnostic candidates.

---

### Task 1: Reconstruct the local market corpus

**Files:**
- Create: `src/prediction_market/local_matching/__init__.py`
- Create: `src/prediction_market/local_matching/facts.py`
- Create: `tests/test_local_matching_facts.py`

**Interfaces:**
- Produces: `MarketFact`, `MarketCorpus`, `load_market_corpus(path: Path) -> MarketCorpus`, and `MarketFact.embedding_text() -> str`.
- Consumes: the current approved-market CSV schema only; no other module or cloud dependency.

- [ ] **Step 1: Write failing corpus tests**

```python
from pathlib import Path

from prediction_market.local_matching.facts import load_market_corpus


def test_loader_deduplicates_each_venue_inside_event_pair(tmp_path: Path) -> None:
    source = tmp_path / "pairs.csv"
    source.write_text(
        "event_match_key,polymarket_market_id,polymarket_yes_token_id,polymarket_event_title,"
        "polymarket_title,outcome_label,polymarket_outcomes,polymarket_settlement_summary,"
        "kalshi_ticker,kalshi_event_title,kalshi_title,kalshi_outcomes,kalshi_settlement_summary\n"
        "e1,p1,y1,World Cup,Will Morocco win?,Morocco,[\\\"Yes\\\",\\\"No\\\"],PM rules,"
        "k1,World Cup,Will Morocco win?,[\\\"Yes: Morocco\\\",\\\"No: Morocco\\\"],KS rules\n"
        "e1,p1,y1,World Cup,Will Morocco win?,Morocco,[\\\"Yes\\\",\\\"No\\\"],PM rules,"
        "k1,World Cup,Will Morocco win?,[\\\"Yes: Morocco\\\",\\\"No: Morocco\\\"],KS rules\n",
        encoding="utf-8",
    )

    corpus = load_market_corpus(source)

    assert len(corpus.pm_by_event["e1"]) == 1
    assert len(corpus.ks_by_event["e1"]) == 1
    assert corpus.candidate_pair_count == 1


def test_embedding_text_excludes_venue_and_native_identifier(tmp_path: Path) -> None:
    corpus = load_market_corpus(_write_single_pair(tmp_path))
    text = corpus.pm_by_event["e1"][0].embedding_text()

    assert "polymarket" not in text.casefold()
    assert "p1" not in text
    assert "will morocco win" in text.casefold()
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `.venv/bin/pytest tests/test_local_matching_facts.py -q`

Expected: collection fails because `prediction_market.local_matching.facts` does not exist.

- [ ] **Step 3: Implement immutable facts and loader**

```python
@dataclass(frozen=True, slots=True)
class MarketFact:
    venue: str
    event_match_key: str
    market_key: str
    native_id: str
    event_title: str
    title: str
    outcome: str
    all_outcomes: str
    settlement_summary: str

    def embedding_text(self) -> str:
        return "\n".join(
            value
            for value in (
                self.event_title.strip(),
                self.title.strip(),
                self.outcome.strip(),
                self.all_outcomes.strip(),
                self.settlement_summary.strip(),
            )
            if value
        )


@dataclass(frozen=True, slots=True)
class MarketCorpus:
    pm_by_event: dict[str, tuple[MarketFact, ...]]
    ks_by_event: dict[str, tuple[MarketFact, ...]]

    @property
    def candidate_pair_count(self) -> int:
        return sum(
            len(pm_rows) * len(self.ks_by_event.get(event_key, ()))
            for event_key, pm_rows in self.pm_by_event.items()
        )
```

`load_market_corpus` must validate the 13 required source columns, construct PM keys as `<market_id>|<yes_token_id>`, construct KS keys from ticker, deduplicate on `(event_match_key, market_key)`, sort keys for deterministic output, and raise `ValueError` listing missing columns.

- [ ] **Step 4: Run the corpus tests and confirm GREEN**

Run: `.venv/bin/pytest tests/test_local_matching_facts.py -q`

Expected: all corpus tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/prediction_market/local_matching tests/test_local_matching_facts.py
git commit -m "feat: reconstruct local market candidate corpus"
```

### Task 2: Extract literal facts and detect contradictions

**Files:**
- Create: `src/prediction_market/local_matching/features.py`
- Create: `tests/test_local_matching_features.py`

**Interfaces:**
- Consumes: `MarketFact` from Task 1.
- Produces: `LiteralFacts`, `PairFeatures`, `extract_literal_facts(fact)`, and `compare_market_facts(pm, ks, semantic_score)`.

- [ ] **Step 1: Write failing feature tests**

```python
def test_different_named_people_are_a_critical_contradiction() -> None:
    pm = fact("polymarket", "Will Oprah Winfrey win the 2028 Democratic presidential nomination?", "Yes")
    ks = fact("kalshi", "Will Michelle Obama be the Democratic Presidential nominee in 2028?", "Yes: Michelle Obama")

    result = compare_market_facts(pm, ks, semantic_score=92.0)

    assert "named_target" in result.critical_contradictions


def test_reach_and_equivalent_above_threshold_are_compatible() -> None:
    pm = fact("polymarket", "Will Bitcoin reach $150,000 by December 31, 2026?", "Yes")
    ks = fact("kalshi", "Will Bitcoin be above $149,999.99 by Dec 31, 2026 at 11:59 PM ET?", "Yes: Above $149,999.99")

    result = compare_market_facts(pm, ks, semantic_score=90.0)

    assert "numeric_threshold" not in result.critical_contradictions
    assert "direction" not in result.critical_contradictions


def test_dip_and_above_are_opposite_directions() -> None:
    pm = fact("polymarket", "Will Bitcoin dip to $25,000 by December 31, 2026?", "Yes")
    ks = fact("kalshi", "Will Bitcoin be above $99,999.99 by Dec 31, 2026 at 11:59 PM ET?", "Yes: Above $99,999.99")

    result = compare_market_facts(pm, ks, semantic_score=88.0)

    assert {"numeric_threshold", "direction"}.issubset(result.critical_contradictions)


def test_map_winner_is_not_whole_match_winner() -> None:
    pm = fact("polymarket", "Valorant: MIBR vs Global Esports - Map 1 Winner", "MIBR")
    ks = fact("kalshi", "Will MIBR win the Global Esports vs. MIBR Valorant match?", "Yes: MIBR")

    result = compare_market_facts(pm, ks, semantic_score=94.0)

    assert "scope" in result.critical_contradictions
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `.venv/bin/pytest tests/test_local_matching_features.py -q`

Expected: import fails because the feature module does not exist.

- [ ] **Step 3: Implement normalization and literal evidence**

`features.py` must define frozen dataclasses with these fields:

```python
@dataclass(frozen=True, slots=True)
class LiteralFacts:
    normalized_title: str
    named_target: str
    significant_numbers: tuple[Decimal, ...]
    years: tuple[int, ...]
    directions: frozenset[str]
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class PairFeatures:
    semantic_score: float
    title_score: float
    settlement_score: float
    target_score: float
    numeric_score: float
    direction_score: float
    temporal_score: float
    scope_score: float
    outcome_score: float
    critical_contradictions: frozenset[str]
```

Normalize only case, Unicode punctuation, whitespace, venue boilerplate, and explicit aliases. Preserve all digits. Treat years separately. Normalize `$150,000 reach` as equivalent to `above $149,999.99` only for cent-denominated strict-above boundaries. Map `dip`, `below`, and `under` to `down`; map `reach`, `above`, and `over` to `up`; retain exact/equality separately. Scope markers must preserve numbered map/set/game values.

Named-target extraction must prefer non-generic outcome text, then parse `Yes: <target>`, then use literal title subjects. Generic terms (`yes`, `no`, `over`, `under`, `other`, `tie`) are not named targets.

- [ ] **Step 4: Implement pair comparison and contradiction reasons**

Use RapidFuzz for title, settlement, target, and outcome similarities. Add critical contradiction names only when both sides provide explicit evidence. Unknown evidence receives a neutral score and must not become a contradiction.

- [ ] **Step 5: Run feature tests and confirm GREEN**

Run: `.venv/bin/pytest tests/test_local_matching_features.py -q`

Expected: all feature tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/prediction_market/local_matching/features.py tests/test_local_matching_features.py
git commit -m "feat: detect literal market contradictions"
```

### Task 3: Add a local-only semantic encoder and cache

**Files:**
- Create: `src/prediction_market/local_matching/encoder.py`
- Create: `tests/test_local_matching_encoder.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `TextEncoder` protocol, `SentenceTransformerEncoder`, `encode_facts`, `load_embedding_cache`, and `write_embedding_cache`.
- Consumes: unique `MarketFact` rows from Task 1.

- [ ] **Step 1: Write failing encoder tests with an in-memory fake**

```python
class FakeEncoder:
    model_name = "fake-local"

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray([[len(text), text.count("win")] for text in texts], dtype=np.float32)


def test_encode_facts_calls_encoder_once_per_unique_text() -> None:
    facts = [fact_with_key("a", "same text"), fact_with_key("b", "same text")]
    vectors = encode_facts(facts, FakeEncoder(), cache={})

    assert set(vectors) == {"a", "b"}
    assert np.array_equal(vectors["a"], vectors["b"])


def test_sentence_transformer_offline_error_names_model(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    encoder = SentenceTransformerEncoder("sentence-transformers/all-MiniLM-L6-v2", local_files_only=True)

    with pytest.raises(RuntimeError, match="all-MiniLM-L6-v2"):
        encoder.encode(["test"])
```

- [ ] **Step 2: Run encoder tests and confirm RED**

Run: `.venv/bin/pytest tests/test_local_matching_encoder.py -q`

Expected: import fails because the encoder module does not exist.

- [ ] **Step 3: Add the optional local dependency**

Add to `pyproject.toml`:

```toml
local-matching = [
  "sentence-transformers>=5.0",
]
```

Do not add any GCP package to this extra.

- [ ] **Step 4: Implement lazy local encoding and cache validation**

`SentenceTransformerEncoder` must import `sentence_transformers` only inside its model loader, pass `local_files_only` through to `SentenceTransformer`, call `encode(..., normalize_embeddings=True)`, and return `float32` arrays. Cache rows are keyed by model name plus SHA-256 of `MarketFact.embedding_text()` and contain model, text hash, dimension, and vector. Reject mixed model names or dimensions.

- [ ] **Step 5: Run encoder tests and confirm GREEN**

Run: `.venv/bin/pytest tests/test_local_matching_encoder.py -q`

Expected: all encoder tests pass without downloading a model.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/prediction_market/local_matching/encoder.py tests/test_local_matching_encoder.py
git commit -m "feat: add cached local semantic encoder"
```

### Task 4: Rank candidates and abstain on weak evidence

**Files:**
- Create: `src/prediction_market/local_matching/ranker.py`
- Create: `tests/test_local_matching_ranker.py`

**Interfaces:**
- Consumes: `MarketCorpus`, vectors keyed by market key, and `compare_market_facts`.
- Produces: `DecisionConfig`, `CandidateScore`, `Top1Decision`, and `rank_corpus(...)`.

- [ ] **Step 1: Write failing ranker tests**

```python
def test_correct_named_candidate_beats_related_wrong_person() -> None:
    pm = fact("polymarket", "Will James Talarico win the 2028 Democratic presidential nomination?", "Yes")
    correct = fact("kalshi", "Will James Talarico be the Democratic Presidential nominee in 2028?", "Yes: James Talarico")
    wrong = fact("kalshi", "Will Michelle Obama be the Democratic Presidential nominee in 2028?", "Yes: Michelle Obama")
    corpus, vectors = corpus_and_vectors(pm, [wrong, correct], semantic=[0.96, 0.90])

    decision = rank_corpus(corpus, vectors, DecisionConfig())[0]

    assert decision.decision == "matched"
    assert decision.kalshi_ticker == correct.native_id


def test_only_contradictory_candidate_returns_no_match() -> None:
    pm = fact("polymarket", "Will Oprah Winfrey win the 2028 Democratic presidential nomination?", "Yes")
    wrong = fact("kalshi", "Will Michelle Obama be the Democratic Presidential nominee in 2028?", "Yes: Michelle Obama")
    corpus, vectors = corpus_and_vectors(pm, [wrong], semantic=[0.97])

    decision = rank_corpus(corpus, vectors, DecisionConfig())[0]

    assert decision.decision == "no_match"
    assert decision.kalshi_ticker == ""
    assert decision.diagnostic_kalshi_ticker == wrong.native_id


def test_small_top1_margin_returns_ambiguous() -> None:
    corpus, vectors = near_tie_fixture()
    config = DecisionConfig(accept_score=78.0, reject_score=62.0, min_margin=4.0)

    decision = rank_corpus(corpus, vectors, config)[0]

    assert decision.decision == "ambiguous"
```

- [ ] **Step 2: Run ranker tests and confirm RED**

Run: `.venv/bin/pytest tests/test_local_matching_ranker.py -q`

Expected: import fails because the ranker module does not exist.

- [ ] **Step 3: Implement deterministic candidate scores**

Use this initial transparent score, then adjust only through benchmark calibration:

```python
score = (
    0.35 * features.semantic_score
    + 0.20 * features.title_score
    + 0.15 * features.target_score
    + 0.08 * features.settlement_score
    + 0.07 * features.numeric_score
    + 0.05 * features.direction_score
    + 0.04 * features.temporal_score
    + 0.04 * features.scope_score
    + 0.02 * features.outcome_score
)
score -= 32.0 * len(features.critical_contradictions)
```

Sort by final score, then target, numeric, scope, title, and ticker for deterministic ties. Preserve the best diagnostic candidate even when it cannot be accepted.

- [ ] **Step 4: Implement the separate decision gate**

Default `DecisionConfig` values are `accept_score=78.0`, `reject_score=62.0`, and `min_margin=4.0`. A candidate with any critical contradiction is never `matched`. If no contradiction-free candidate exists, return `no_match`. A contradiction-free candidate below reject score is `no_match`; between reject and accept or below margin is `ambiguous`; otherwise it is `matched`.

- [ ] **Step 5: Run ranker tests and confirm GREEN**

Run: `.venv/bin/pytest tests/test_local_matching_ranker.py -q`

Expected: all ranker tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/prediction_market/local_matching/ranker.py tests/test_local_matching_ranker.py
git commit -m "feat: rerank local markets with abstention"
```

### Task 5: Create independent benchmark and evaluation

**Files:**
- Create: `data/cross_sports_arbitrage/manual_review/local_match_benchmark/current.csv`
- Create: `src/prediction_market/local_matching/evaluate.py`
- Create: `tests/test_local_matching_evaluate.py`

**Interfaces:**
- Produces: `load_benchmark`, `evaluate_decisions`, and a metrics dictionary containing baseline/new precision, coverage, no-match recall, ambiguous rate, and grouped errors.
- Consumes: `Top1Decision` rows and benchmark columns `case_id,event_match_key,pm_market_key,expected_ks_ticker,expected_decision,split,rationale`.

- [ ] **Step 1: Write failing evaluation tests**

```python
def test_metrics_separate_precision_from_coverage() -> None:
    labels = pd.DataFrame(
        [
            {"pm_market_key": "p1", "expected_ks_ticker": "k1", "expected_decision": "matched"},
            {"pm_market_key": "p2", "expected_ks_ticker": "", "expected_decision": "no_match"},
            {"pm_market_key": "p3", "expected_ks_ticker": "k3", "expected_decision": "matched"},
        ]
    )
    decisions = [matched("p1", "k1"), no_match("p2"), ambiguous("p3", "k3")]

    metrics = evaluate_decisions(decisions, labels)

    assert metrics["accepted_top1_precision"] == 1.0
    assert metrics["accepted_coverage"] == 1 / 3
    assert metrics["no_match_recall"] == 1.0
```

- [ ] **Step 2: Run evaluation tests and confirm RED**

Run: `.venv/bin/pytest tests/test_local_matching_evaluate.py -q`

Expected: import fails because the evaluation module does not exist.

- [ ] **Step 3: Add manually checked benchmark rows from current data**

Include both train and holdout rows. The required regression rows are:

```csv
case_id,event_match_key,pm_market_key,expected_ks_ticker,expected_decision,split,rationale
world_cup_morocco,30615__KXMENWORLDCUP-26,0x37a6de1b21803e5f3fb1965116218215d79963af4f7e51659696366267a63a03|69910730841487615802736046038473620030754616421912831175284551372639933569112,KXMENWORLDCUP-26-MA,matched,holdout,Same team and tournament winner
dem_nominee_james_talarico,30829__KXPRESNOMD-28,0xb91be12388b3d4079c3ed9b5783cb42d8c33051d37746a49300227e0f45fc089|52535923606561722941567320365820395300598958985353103429657683100920373025261,KXPRESNOMD-28-JTAL,matched,holdout,Same person party office and year
dem_nominee_oprah,30829__KXPRESNOMD-28,0xe06a7e94cf2fa8dc2085b7610fe16e9be1cde6654f34d365c13da1149b276c61|2213957649161627793381994368131485505647723208738124952452819345058597751695,,no_match,holdout,Only saved candidate names Michelle Obama
bitcoin_reach_150k,89502__KXBTCMAXY-26DEC31,0xa7b594ae07d5c1590fa86028fcc2f8705990437237416556c05837a08b2e1cda|9408196828451163378822245032645030045707991112669125056198742225498158094445,KXBTCMAXY-26DEC31-149999.99,matched,holdout,Above 149999.99 is the cent boundary for reaching 150000
bitcoin_dip_25k,89502__KXBTCMAXY-26DEC31,0xe326d1abf5fb59b82ecfdff3348e75f90561eace327ba1bdc8d38d045ddbe775|58908160299895538838177673280060816284346493901538403975218911918392404378292,,no_match,holdout,Dip to 25000 is not any above-price contract
valorant_map1_mibr,644413__KXVALORANTGAME-26JUL030700GEMIBR,0x9f5380b99f9d368b9b821432bc3b8ef633e817f6e5d9f999d10d9c28090ab97b|12588043380505599082086381710951936985418475471832322325361008334899286630998,,no_match,holdout,Map 1 winner differs from whole match winner
nba_2027_pelicans,478277__KXNBA-27,0x222084d0539e55927b8f2b22735017daecaa996b63645886caba07513061fc16|38556205473470072936293737937663401559968429300207925799805811020804034073896,KXNBA-27-NOP,matched,train,Same team season and championship
fed_zero_cuts,51456__KXRATECUTCOUNT-26DEC31,0xd4e77ba6f29fc093509d24f508631abd445ecf506bbdc9c4c80e60256a318527|12403602920039269077597917340921667997547115084613238528792639013246536343316,KXRATECUTCOUNT-26DEC31-T0,matched,train,No cuts equals exactly zero cuts
f1_oscar_champion,100371__KXF1-26,0x1c373746c204f7437cb212d2bcfbc5fdf0f0263ae9b470dd92c676d4c2c5ba03|45035925547723382374916856041538026483546248029152755914741212781718674476988,KXF1-26-OP,matched,train,Same driver and season championship
```

These nine rows form the initial independent regression benchmark. Add a newly discovered failure to this file before changing extraction, scoring, or thresholds for that failure; never derive expected labels from `manual_decision`.

- [ ] **Step 4: Implement evaluator and baseline comparison**

The baseline ticker is the existing saved ticker for each benchmark PM key. Count only emitted `matched` rows in accepted precision. Report all denominators explicitly and list every false accepted match with case ID and contradiction diagnostics.

- [ ] **Step 5: Run evaluation tests and confirm GREEN**

Run: `.venv/bin/pytest tests/test_local_matching_evaluate.py -q`

Expected: all evaluation tests pass.

- [ ] **Step 6: Commit**

```bash
git add data/cross_sports_arbitrage/manual_review/local_match_benchmark/current.csv src/prediction_market/local_matching/evaluate.py tests/test_local_matching_evaluate.py
git commit -m "test: add independent local matching benchmark"
```

### Task 6: Build the local CLI and write diagnostic outputs

**Files:**
- Create: `src/prediction_market/local_market_match.py`
- Create: `tests/test_local_market_match_cli.py`
- Modify: `pyproject.toml`
- Modify: `README.md`

**Interfaces:**
- Consumes all Task 1-5 interfaces.
- Produces `main(argv: list[str] | None = None) -> int` and the five output artifacts specified by the design.

- [ ] **Step 1: Write failing CLI integration tests**

```python
def test_cli_with_fake_encoder_writes_all_local_outputs(tmp_path: Path) -> None:
    source = write_integration_source(tmp_path)
    benchmark = write_integration_benchmark(tmp_path)

    result = run_local_match(
        source=source,
        benchmark=benchmark,
        output_dir=tmp_path / "out",
        encoder=FakeEncoder(),
    )

    assert result == 0
    assert (tmp_path / "out/local_market_match_candidates.csv").exists()
    assert (tmp_path / "out/local_market_match_top1.csv").exists()
    assert (tmp_path / "out/local_market_match_no_match.csv").exists()
    assert (tmp_path / "out/local_market_match_metrics.json").exists()


def test_cli_has_no_gcp_provider_option() -> None:
    parser = build_parser()
    help_text = parser.format_help().casefold()

    assert "vertex" not in help_text
    assert "gemini" not in help_text
    assert "gcp" not in help_text
```

- [ ] **Step 2: Run CLI tests and confirm RED**

Run: `.venv/bin/pytest tests/test_local_market_match_cli.py -q`

Expected: import fails because the CLI module does not exist.

- [ ] **Step 3: Implement CLI orchestration and atomic output writes**

Arguments must include source path, benchmark path, output directory, model name, `--allow-model-download`, top-k, accept score, reject score, and minimum margin. Default paths point to current local data and `data/cross_sports_arbitrage/processed/latest`. Default model loading is offline (`local_files_only=True`). Write each artifact to a temporary sibling and replace it after successful serialization.

- [ ] **Step 4: Add the project script and README command**

Add:

```toml
poly-x-kalshi-local-market-match = "prediction_market.local_market_match:main"
```

Document these commands:

```bash
pip install -e '.[dev,local-matching]'
poly-x-kalshi-local-market-match --allow-model-download
poly-x-kalshi-local-market-match
```

The first command permits the one-time model fetch; the second matching run proves cached local execution.

- [ ] **Step 5: Run CLI tests and confirm GREEN**

Run: `.venv/bin/pytest tests/test_local_market_match_cli.py -q`

Expected: all CLI tests pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml README.md src/prediction_market/local_market_match.py tests/test_local_market_match_cli.py
git commit -m "feat: add local market matching command"
```

### Task 7: Run, calibrate, and verify the complete local matcher

**Files:**
- Modify only if benchmark evidence requires it: `src/prediction_market/local_matching/features.py`
- Modify only if benchmark evidence requires it: `src/prediction_market/local_matching/ranker.py`
- Modify: `data/cross_sports_arbitrage/manual_review/local_match_benchmark/current.csv`
- Generated and ignored: `data/cross_sports_arbitrage/processed/latest/local_market_match_*`

**Interfaces:**
- Consumes the completed CLI and benchmark.
- Produces verified local metrics and full reranked current-data outputs.

- [ ] **Step 1: Install only the local matching extra**

Run: `.venv/bin/pip install -e '.[dev,local-matching]'`

Expected: sentence-transformers and its local runtime dependencies install; no GCP package is installed or invoked.

- [ ] **Step 2: Run the targeted regression suite**

Run: `.venv/bin/pytest tests/test_local_matching_facts.py tests/test_local_matching_features.py tests/test_local_matching_encoder.py tests/test_local_matching_ranker.py tests/test_local_matching_evaluate.py tests/test_local_market_match_cli.py -q`

Expected: all targeted tests pass.

- [ ] **Step 3: Download the model once and run the full current corpus**

Run: `.venv/bin/poly-x-kalshi-local-market-match --allow-model-download`

Expected: 10,959 PM rows and 115,858 reconstructable candidates are processed, and all five local outputs are written.

- [ ] **Step 4: Prove the second run is offline**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/poly-x-kalshi-local-market-match`

Expected: the run succeeds using cached model files and produces identical top-1 decisions and metrics.

- [ ] **Step 5: Calibrate only on training labels**

Run a small grid over accept score `74, 78, 82, 86` and minimum margin `2, 4, 6, 8`. Select the configuration with zero training false accepts and greatest training coverage. Do not inspect holdout results while selecting.

- [ ] **Step 6: Evaluate holdout and inspect every error**

Run the selected configuration once against the holdout split. The gate is zero known targeted regression failures and accepted top-1 precision higher than the forced baseline. Record coverage and all abstentions; do not weaken contradiction rules merely to increase coverage.

- [ ] **Step 7: Run the complete existing test suite**

Run: `.venv/bin/pytest -q`

Expected: all existing and new tests pass.

- [ ] **Step 8: Check repository and cloud-call isolation**

Run:

```bash
rg -n "vertex|gemini|google.cloud|aiplatform" src/prediction_market/local_matching src/prediction_market/local_market_match.py
git diff --check
git status --short
```

Expected: the search returns no cloud references, diff check is clean, and only intentional source/test/benchmark changes remain.

- [ ] **Step 9: Commit calibrated configuration and benchmark additions**

```bash
git add src/prediction_market/local_matching data/cross_sports_arbitrage/manual_review/local_match_benchmark/current.csv
git commit -m "fix: calibrate local top-one market matching"
```
