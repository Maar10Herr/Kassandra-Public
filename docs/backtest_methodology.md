# Point-in-time evaluation contract

## Status

Kassandra has a fail-closed evaluation contract, not a populated validated
benchmark. No real historical result is supplied by this repository and no
validated-product performance claim may be made from it.

`src/kassandra/benchmark.py` contains five hand-picked distressed cases. They
remain usable only for classifier/regression exploration. They are not a
point-in-time cohort: they have no matched controls, immutable source manifest,
or frozen independent corpus.

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
both direct-only and graph-enhanced scorers. This prevents a partial path from
being presented as a graph comparison.

`allow_exploratory=True` is an explicit research-only escape hatch. It returns
`exploratory_unvalidated`, preserves the validation issues, and emits no
metrics. The default is fail-closed via `ValidationContractError`.

## Chronological comparison and outputs

For each case, each scorer receives only documents whose publication and
retrieval times are no later than the case outcome date. The contract reports
separate direct-only and graph-enhanced case-level outputs:

- denominators: distressed, controls, total;
- confusion counts: TP, FP, FN, TN;
- precision, recall, and specificity where denominators are non-zero; and
- lead-time days for true-positive distressed cases where eligible source
  timestamps exist.

The scorer functions are supplied by the caller, so a future graph-enabled
scorer must be identified in frozen metadata and can be compared against its
direct-only counterpart without silently changing the corpus or chronology.

## Before a product claim

Curate independently sourced historical evidence for both cohorts; retain raw
provenance and immutable manifests; freeze code/config identifiers before
scoring; then execute and review a complete manifest. Do not infer unavailable
publication/retrieval times or outcomes, backfill them from hindsight, or turn
the existing five-case exploration output into a performance result.
