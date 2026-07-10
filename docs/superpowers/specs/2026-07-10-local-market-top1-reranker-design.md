# Local Market Top-1 Reranker Design

## Objective

Improve market-level Polymarket-to-Kalshi matching so an emitted top-1 pair is a resolution-equivalent contract, while running entirely on the local machine. When the current candidate universe has no sufficiently supported equivalent contract, the matcher must emit `no_match` instead of forcing a nearest neighbor.

## Current-State Findings

The current `approved_market_pairs/current.csv` contains 10,959 Polymarket market/outcome rows grouped into 1,320 event pairs and references 5,553 Kalshi market rows. Reconstructing unique venue rows inside each event pair produces 115,858 possible market pairs.

The saved rows are not independent ground truth. They were bulk-promoted from embedding top-1 output and include clear contradictions, such as a different named person, a different numerical threshold or direction, and a map-level contract matched to a whole-match winner. The existing scorer uses embedding cosine alone and always returns a candidate, so topic similarity can override settlement equivalence.

## Scope

The first implementation reranks market contracts only inside the already paired event containers represented by the current local data. It does not refresh venue data, change event-pair decisions, call a hosted model, overwrite approval files, or deploy infrastructure.

The implementation must:

- run without GCP credentials, Vertex AI packages, or cloud API calls;
- reconstruct the complete locally available candidate cross-product per event pair;
- derive features only from venue-provided titles, outcomes, rules, settlement summaries, identifiers, and dates;
- preserve contradictory facts rather than smoothing them into a single semantic score;
- distinguish `matched`, `no_match`, and `ambiguous` decisions;
- write new output files under the ignored `processed/latest` directory;
- report top-1 precision, coverage, and error categories against independent local labels.

## Approaches Considered

### Fuzzy matching only

Word and character similarity is fast and interpretable, but it cannot reliably distinguish opposing directions, nearby thresholds, or map-versus-match scope. It is useful as a feature, not as the final decision rule.

### Local semantic embedding only

A small sentence-transformer improves wording tolerance, but embedding similarity still treats related contracts as interchangeable. This repeats the root problem unless literal contradictions are scored separately.

### Hybrid local reranking

Use a local sentence-transformer for semantic retrieval, combine it with literal fact compatibility, and fail closed when evidence is weak or contradictory. This is the selected approach because it preserves semantic recall while making equivalence decisions explainable.

## Architecture

### Local corpus loader

`local_market_match.py` reads `manual_review/approved_market_pairs/current.csv`, deduplicates Polymarket rows by market ID plus YES token, deduplicates Kalshi rows by ticker, and groups both sides by `event_match_key`. It validates required columns and reports groups whose locally available candidate side is empty.

### Venue fact representation

Each market becomes an immutable `MarketFact` containing the original venue fields plus normalized views used for comparison. Normalization is limited to case, punctuation, Unicode, whitespace, common venue boilerplate, and unambiguous aliases such as `men's world cup` and `FIFA world cup`.

Literal extractors produce evidence rather than subjective market categories:

- named target tokens from outcome labels and the title portion that differs from the event title;
- numbers with nearby currency, percentage, score, count, or threshold context;
- explicit comparators such as above, below, over, under, reach, dip, win, and lose;
- explicit temporal expressions and years;
- explicit scope markers such as map, set, game, match, series, half, quarter, and tournament;
- outcome-side text supplied by the venue.

The existing `market_type` column may be included in diagnostics but must not decide equivalence or serve as a label.

### Local semantic encoder

The default encoder is `sentence-transformers/all-MiniLM-L6-v2`, executed locally. Model files live in the user's local model cache. The matching command has no GCP code path. A one-time model download is allowed only when explicitly requested; cached/offline execution is the normal path.

Unique market texts are embedded once and cached locally by model name plus text hash. The text includes event title, market title, outcome/side, rules or settlement summary, and close/expiration information, but excludes venue names and native identifiers because those fields add platform-specific noise.

### Candidate scoring

All locally available Kalshi markets inside the paired event container are candidates. Each pair receives separate feature values:

- semantic cosine similarity;
- title word and character similarity;
- settlement/rules similarity;
- named-target compatibility;
- number and unit compatibility;
- comparator/direction compatibility;
- temporal compatibility;
- scope compatibility;
- outcome-side compatibility.

Critical contradictions are retained as named reasons. A candidate with a different named target, incompatible non-date threshold, opposite direction, or incompatible explicit scope cannot be accepted as equivalent even if its semantic similarity is high. It may remain in the diagnostic candidate output.

The final ranking score combines semantic and lexical evidence with literal compatibility. Critical contradictions apply a strong penalty. Exact weights and thresholds are configuration values calibrated on the training portion of the local benchmark, not constants inferred from the bulk-approved output.

### Decision layer

For each Polymarket row, the scorer returns ranked diagnostics and then applies a separate acceptance gate:

- `matched`: no critical contradiction, score at or above the calibrated threshold, and sufficient margin over rank 2;
- `ambiguous`: no critical contradiction but threshold or margin evidence is insufficient;
- `no_match`: every candidate has a critical contradiction or the best score is below the rejection threshold.

Only `matched` rows count as emitted top-1 matches. `ambiguous` and `no_match` retain their best diagnostic candidate but do not present it as an equivalent pair.

## Evaluation

The current bulk-approved file is input data, not truth. A separate benchmark CSV records Polymarket identity, expected Kalshi ticker or `no_match`, label rationale, and split. It must include:

- known current failures involving people, teams, thresholds, direction, dates, and scope;
- representative exact winner, nominee, price-threshold, totals, and match contracts;
- events with no listed equivalent;
- difficult near-neighbor candidates from the same event container.

Thresholds are tuned only on the training split. The holdout split reports:

- accepted top-1 precision;
- accepted coverage;
- false-match count;
- no-match recall;
- ambiguous rate;
- errors grouped by contradiction type.

The primary gate is zero known false matches in the targeted regression suite and higher holdout top-1 precision than the current forced-top-1 baseline. Coverage is secondary: the matcher should abstain rather than manufacture equivalence.

## Outputs

The local command writes:

- `local_market_match_candidates.csv`: ranked candidates and every feature/reason;
- `local_market_match_top1.csv`: one decision per Polymarket market/outcome;
- `local_market_match_no_match.csv`: rejected and ambiguous rows;
- `local_market_match_metrics.json`: corpus, precision, coverage, and error metrics;
- `local_market_embeddings.parquet`: optional cached local vectors.

Existing approval files remain unchanged.

## Error Handling

The command fails with a clear message when required columns are absent, no candidate rows can be reconstructed, a requested local model is unavailable in offline mode, or cached vectors do not match the configured model and dimension. Individual malformed venue rows are recorded with an error reason and excluded from accepted matches.

## Testing

Tests follow a red-green cycle and cover candidate reconstruction, normalization, literal extraction, contradiction detection, ranking, margin-based abstention, output schemas, and the CLI's prohibition on cloud providers. Regression cases explicitly include the current Oprah/Michelle, Bitcoin dip/above, map/match, and correct World Cup winner examples.

The complete local test suite and a benchmark run must pass before replacing any downstream consumer with the new output.
