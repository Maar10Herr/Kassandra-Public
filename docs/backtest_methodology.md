# Point-in-time evaluation contract

## Status

Kassandra has a fail-closed evaluation contract. The repository contains no
populated validated benchmark or real historical result; its contents support
no product-performance claim.

`src/kassandra/benchmark.py` contains five hand-picked distressed cases for
classifier and regression testing. They lack matched controls, an immutable
source manifest, and a frozen independent corpus, so they are not an evaluation
cohort.

## Required manifest

Use `kassandra.backtest.PointInTimeManifest`. Its immutable dataclasses require:

- a manifest identifier and explicit timezone-aware `as_of` timestamp;
- `CohortCase` records labelled `distressed` or `control`, each with sector,
  matched-group identifier, and explicit outcome date;
- every matched group to contain distressed cases and non-distressed controls,
  share one sector, and have outcome dates within the configured matching window
  (366 days by default);
- `EvaluationDocument` records with publication and retrieval timestamps;
- frozen direct-scorer, graph-scorer, classifier, and configuration identifiers; and
- a SHA-256 hash of the canonical complete document corpus.

The runner rejects manifests when publication or retrieval is later than
`as_of`, dates/controls/metadata are absent, the supplied corpus differs from
its frozen hash, or a document references an unknown case. It also requires
both direct-only and graph-enhanced scorers, so comparisons use complete paths.

`allow_exploratory=True` selects research-only mode. It returns
`exploratory_unvalidated`, preserves the validation issues, and emits no
metrics. Default behavior is fail-closed via `ValidationContractError`.

## Chronological comparison and outputs

For each case, each scorer receives only documents whose publication and
retrieval times are no later than the case outcome date. The contract reports
separate direct-only and graph-enhanced case-level outputs:

- denominators: distressed, controls, total;
- confusion counts: TP, FP, FN, TN;
- precision, recall, and specificity where denominators are non-zero; and
- lead-time days for true-positive distressed cases where eligible source
  timestamps exist.

The scorer functions are supplied by the caller. A future graph-enabled scorer
must be identified in frozen metadata and compared with its direct-only
counterpart using the same corpus and chronology.

## Before reporting performance

Curate independently sourced historical evidence for both cohorts; retain raw
provenance and immutable manifests; freeze code/config identifiers before
scoring; then execute and review a complete manifest. Use recorded
publication/retrieval times and outcomes only. Hindsight backfilling or treating
the five-case exploration as a performance result invalidates the evaluation.
