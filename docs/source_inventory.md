# Source Inventory

Release snapshot: August 2026. “Active” means an adapter exists in this
release; it does not guarantee current availability, completeness, or
permission for a particular deployment. Verify source terms and access before
use.

| Source | Type | Status | Coverage | Unique Evidence | Notes |
|--------|------|--------|----------|-----------------|-------|
| Companies House REST API | Official registry + filings | 🟢 Active | UK-registered entities | Filing history, company profiles | Rate-limited (1 req/s, 500/day). Basic auth. |
| UK Gazette insolvency feed | Official gazette (RSS) | 🟢 Active | UK entities | Insolvency, winding-up, administration notices | Free, no auth. Daily RSS feed. |
| Handelsregister.de | Official registry (JSF) | 🟡 Active (EU IP only) | DE-registered entities | Register status, insolvency markers | mechanize-based. Blocks non-EU IPs. No API key needed. |
| BODACC | Official gazette (API) | 🟢 Active | FR-registered entities | Procédures collectives, radiations | Free JSON API. No auth. Monitors insolvency + strike-off families. |
| BORME | Official gazette (RSS) | 🟢 Active | ES-registered entities | Liquidación, concurso, disolución, reducción de capital, fusión, escisión | Free RSS feed. No auth. Fetch-once-filter-locally pattern. |
| GLEIF | LEI reference data | 🟢 Active | Global (LEI-holding entities) | Legal name, address, relationship data | Free API. Used for entity resolution, not event detection. |
| Web Monitor | Web pages + feeds | 🟢 Scheduled | Company IR pages, sitemaps | Arbitrary regulatory/news content | Content-classifier downstream. Supervised bounded child in the daily soak; freshness SLA is 26 hours. |

## Scheduling contract

The scheduled source set is intentional and is the only set whose freshness is
required for a green soak/report:

- **Scheduled by daily soak:** `companies_house`, `uk_gazette`, `web_monitor`.
  The real-time daemon additionally polls `bodacc_fr` and `borme_es` every 15
  minutes together with Companies House and the Gazette. Every scheduled source
  must have a recent completed `source_runs` row with `status=success`; missing,
  failed, or older-than-26-hour runs make the cycle degraded.
- **Manual-only (not a freshness failure when absent):** `handelsregister_de`.
  This adapter is available for explicit collection and is environment-dependent
  because the site blocks non-EU IPs.

This distinction prevents an absent schedule from being reported as healthy based
only on historical evidence, while avoiding false failures for intentionally
manual sources.


Each source is evaluated on:
- **Coverage**: Which entities/jurisdictions does it cover?
- **Timeliness**: How quickly does it surface new information?
- **Unique evidence**: Does it provide signals not available elsewhere?
- **Reliability**: False positive rate, data quality
- **Cost**: Network requests, storage, parsing complexity
- **Incremental chronological value**: Does it improve lead time or precision?
