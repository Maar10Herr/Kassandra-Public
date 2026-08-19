# Kassandra

Rule-based corporate early-warning and dependency intelligence for analyst-led
research.

> **Software status**
>
> Experimental research implementation. Kassandra is not a validated credit
> model, credit-rating system, trading signal, or production decision engine.
> This repository supplies no estimate of precision, recall, lead time, or
> financial performance. Every alert requires human review of the underlying
> evidence and entity match.

Kassandra collects public corporate records and public-web material, preserves
source provenance, applies deterministic event classifiers, constructs a typed
entity graph, and produces two deliberately separate rankings:

- `active_watch_priority` is gated by adverse evidence and is zero without an
  active direct or eligible transmitted signal;
- `coverage_monitor_priority` describes information and graph-coverage gaps,
  not credit deterioration.

The distinction prevents graph density or missing data from being presented as
credit risk.

## Architecture

```text
public sources -> evidence store -> deterministic classifier -> events
                      |                                      |
                      v                                      v
              provenance and hashes              entity/dependency graph
                                                             |
                                                             v
                                  watch priority + coverage monitoring
                                                             |
                                                             v
                                           CLI, dashboard, Markdown digest
```

The implementation uses a local SQLite database and content-addressed evidence
store. Neither is included in this repository. Source adapters cover Companies
House, the UK Gazette, GLEIF, BODACC, BORME, E-PRTR, TED, selected public web
pages and feeds, and an environment-dependent Handelsregister adapter. Access,
coverage, rate limits, robots policies, and terms remain source- and
jurisdiction-specific; inclusion of an adapter does not imply endorsement or
guaranteed availability.

## What is included

- Python 3.11+ package and `kassandra` command-line interface
- deterministic evidence classification and provenance records
- legal-entity resolution and typed relationship graph construction
- gated scoring with reason codes and component-level audit fields
- local dashboard, scheduled-cycle, and alert-formatting paths
- schema migrations and synthetic/in-memory tests
- a fail-closed point-in-time evaluation contract
- a synthetic scoring-cost benchmark

Runtime databases, collected evidence, portfolios, environment files, logs,
backups, schedules, and operator state are excluded by design.

## Installation

Using [`uv`](https://docs.astral.sh/uv/):

```sh
uv sync --extra dev
uv run kassandra --help
```

Or with a Python virtual environment:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
kassandra --help
```

Create a project-local `.env` only when a source requires credentials. For
example:

```text
COMPANIES_HOUSE_API_KEY=replace_with_your_own_key
```

Never commit `.env`, a database, or collected evidence. The application does
not contain embedded service credentials and does not load credentials from
user- or machine-wide configuration directories.

## Minimal local workflow

```sh
uv run kassandra init
uv run kassandra import-portfolio
uv run kassandra resolve
uv run kassandra collect
uv run kassandra build-graph
uv run kassandra score
uv run kassandra dashboard --port 8765
```

Collection performs network requests to the configured public sources. Start
with a disposable local database, review source-specific documentation, and
respect applicable access conditions. Use `kassandra evidence-show ID` to
inspect the provenance behind an event before treating it as analyst input.

## Evaluation boundary

The five distressed-company cases in `src/kassandra/benchmark.py` are curated,
positive-only regression material. They were assembled and used during pattern
development; they are not an independent historical cohort and provide no
false-positive denominator.

[`docs/backtest_methodology.md`](docs/backtest_methodology.md) defines a
fail-closed point-in-time contract for future evaluation. It requires matched
distressed and non-distressed cases, dated documents, frozen model/configuration
identifiers, and a complete corpus hash. If those inputs are missing, the
runner emits no performance metrics.

Accordingly, this release makes no claim that Kassandra predicts defaults,
improves lead time, establishes causal transmission, or outperforms direct-only
monitoring.

## Scoring semantics

The current scoring layer is explainable triage, not probability estimation.

```text
active_watch_priority
    = adverse_signal x credibility x materiality x recency

coverage_monitor_priority
    = graph coverage + information gap + source staleness
```

Only eligible evidence and relationship tiers enter watch transmission. Unknown
materiality remains explicit; it is not silently converted into certainty.
Inspect the reason codes and evidence record rather than relying on the scalar
score alone.

## Documentation

- [`docs/data_dictionary.md`](docs/data_dictionary.md) — schema and event taxonomy
- [`docs/source_inventory.md`](docs/source_inventory.md) — source roles and scheduling contract
- [`docs/country_registry_matrix.md`](docs/country_registry_matrix.md) — dated jurisdiction/access survey
- [`docs/backtest_methodology.md`](docs/backtest_methodology.md) — validation contract and claim limits

## Verification

```sh
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src tests
```

The portable cost benchmark uses synthetic rows and does not measure model
quality:

```sh
uv run python scripts/benchmark_scoring_scale.py \
  --sizes 50 500 3000 --repeats 3
```

GitHub Actions runs the test suite and a syntax check on Python 3.11 and 3.13.
Network collectors are intentionally not exercised against live services in CI.

## Security and data handling

Kassandra processes potentially sensitive analyst portfolios and collected
documents when operated locally. This public repository contains no such data.
Operators should:

- use a dedicated environment and least-privilege source credentials;
- keep `.env`, `data/`, SQLite files, evidence objects, and backups outside
  version control;
- review source URLs and content before distributing an alert;
- treat entity resolution and event classification as fallible;
- deploy the dashboard only behind controls appropriate to their environment.

The implementation has not received an external security audit.

## Repository layout

```text
src/kassandra/       package, CLI, collectors, graph, scoring, dashboard
state/migrations/    schema-only SQLite migrations
tests/               synthetic and in-memory regression/contract tests
docs/                public schema, source, and evaluation documentation
scripts/             synthetic scoring-cost benchmark
config/              non-secret defaults
```

## Citation and attribution

Kassandra integrates public information from independent registries, gazettes,
reference-data services, procurement systems, and company websites. Those
sources retain their own ownership and terms; this repository neither republishes
a collected dataset nor claims authorship of source records. See the source
inventory for the role assigned to each adapter.

If you use the software in research, cite the versioned GitHub release using
[`CITATION.cff`](CITATION.cff).

## License

The Kassandra source in this repository is released under the [MIT License](LICENSE).
Third-party dependencies remain subject to their respective licenses.
