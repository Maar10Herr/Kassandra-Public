# Data Dictionary

## Core Tables

### `portfolios`
Group of root companies (e.g., "Euro Stoxx 50").

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| name | TEXT | Portfolio name |
| created_at | TEXT | ISO 8601 timestamp |

### `portfolio_items`
Individual companies in a portfolio.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| portfolio_id | INTEGER FK | References portfolios.id |
| ticker | TEXT | Stock ticker symbol |
| isin | TEXT UNIQUE | 12-character ISIN identifier |
| name | TEXT | Full company name |
| sector | TEXT | GICS sector |
| country | TEXT | ISO 3166-1 alpha-2 |
| weight | REAL | Portfolio weight |
| source | TEXT | Data source ("pinned_list", etc.) |
| imported_at | TEXT | ISO 8601 timestamp |

### `registry`
Canonical company identities.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| canonical_name | TEXT | Primary legal name |
| companies_house_number | TEXT UNIQUE | UK Companies House ID |
| lei | TEXT UNIQUE | Legal Entity Identifier |
| isin | TEXT | 12-character ISIN |
| jurisdiction | TEXT | Country code |
| company_type | TEXT | Legal form |
| status | TEXT | active/dissolved/etc. |
| incorporation_date | TEXT | Date of incorporation |
| registered_address | TEXT | Full address |
| raw_json | TEXT | Source profile as JSON |
| resolved_at | TEXT | First resolution timestamp |
| updated_at | TEXT | Last update timestamp |

### `evidence`
Immutable content-addressed evidence items.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| content_hash | TEXT UNIQUE | SHA-256 of content |
| source_url | TEXT | Canonical source URL |
| retrieval_time | TEXT | When content was fetched |
| publication_time | TEXT | When content was published |
| publication_time_confidence | TEXT | Certainty level |
| first_seen_time | TEXT | First time observed |
| extraction_method | TEXT | Adapter name |
| parser_version | TEXT | Parser version |
| content_type | TEXT | MIME type |
| content_length | INTEGER | Content size in bytes |
| excerpt | TEXT | Relevant excerpt (max 2000 chars) |
| source_reliability | REAL | 0-1 reliability score |
| corroborated_by | TEXT | Supporting evidence IDs |
| raw_headers | TEXT | HTTP response headers |
| created_at | TEXT | Record creation timestamp |

### `events`
Material events linked to evidence and registry.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| evidence_id | INTEGER FK | References evidence.id |
| registry_id | INTEGER FK | References registry.id |
| event_type | TEXT | Event taxonomy type |
| event_subtype | TEXT | Sub-classification |
| severity | TEXT | critical/high/medium/low |
| confidence | REAL | 0-1 |
| description | TEXT | Human-readable summary |
| extracted_at | TEXT | Extraction timestamp |
| source_claims_directly | BOOLEAN | True if source states it explicitly |
| raw_event_json | TEXT | Raw event data |

### `edges`
Dependency graph relationships.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| source_registry_id | INTEGER FK | From entity |
| target_registry_id | INTEGER FK | To entity |
| relationship_type | TEXT | parent/subsidiary/customer/supplier/etc. |
| evidence_id | INTEGER FK | Supporting evidence |
| confidence | REAL | 0-1 |
| economic_materiality | REAL | Financial significance |
| operational_criticality | REAL | Operations dependency |
| concentration | REAL | Single-source risk |
| replaceability | REAL | Ease of substitution |
| switching_time_days | INTEGER | Time to switch |
| inventory_buffer_days | INTEGER | Inventory cushion |
| payment_lag_days | INTEGER | Payment delay |
| shock_channels | TEXT | Applicable transmission channels |
| is_reversible | BOOLEAN | True if edge is bidirectional |
| valid_from | TEXT | Relationship start date |
| valid_until | TEXT | Relationship end date |
| created_at | TEXT | Record creation timestamp |

### `scores`
Immutable scoring snapshots.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| registry_id | INTEGER FK | References registry.id |
| score_schema_version | INTEGER | Scoring model version |
| observation_severity | REAL | Event-driven severity |
| deterioration_risk | REAL | Modeled risk (0-1) |
| dependency_exposure | REAL | Graph-based exposure |
| analyst_priority | REAL | Combined priority (0-1) |
| factors_json | TEXT | Contributing factors as JSON |
| explanation | TEXT | Human-readable explanation |
| computed_at | TEXT | Computation timestamp |

### `sources`
Source health tracking.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| source_name | TEXT UNIQUE | Source identifier |
| source_type | TEXT | api/feed/scraper |
| base_url | TEXT | Base URL |
| last_success_at | TEXT | Last successful fetch |
| last_failure_at | TEXT | Last failure |
| consecutive_failures | INTEGER | Failure streak |
| total_requests | INTEGER | Lifetime requests |
| total_evidence | INTEGER | Evidence items collected |
| unique_events | INTEGER | Unique events detected |
| status | TEXT | active/paused/disabled |
| created_at | TEXT | Record creation timestamp |

### `job_state`
Resumable job state for crash recovery.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| job_name | TEXT UNIQUE | Job identifier |
| status | TEXT | idle/running/completed/failed |
| last_run_at | TEXT | Last execution |
| last_error | TEXT | Last error message |
| next_run_at | TEXT | Scheduled next run |
| payload_json | TEXT | Job parameters |
| created_at | TEXT | Record creation timestamp |

## Event Taxonomy

| Event Type | Severity | Description |
|-----------|----------|-------------|
| insolvency | critical | Insolvency, winding-up, liquidation, administration |
| restructuring | high | Capital reduction, voluntary arrangement |
| going_concern_warning | high | Auditor going concern qualification |
| auditor_warning | high | Qualified audit report |
| auditor_departure | medium | Auditor resignation/removal |
| payment_stress | high | Covenant breach, payment default |
| refinancing_stress | high | Emergency refinancing |
| emergency_capital | high | Emergency capital action |
| profit_warning | medium | Profit warning |
| guidance_withdrawal | medium | Guidance withdrawn |
| late_reporting | medium | Accounts overdue |
| revised_reporting | medium | Restated financials |
| management_departure | low | Director/officer resignation |
| hiring_freeze | low | Hiring freeze |
| layoffs | medium | Redundancies, short-time work |
| facility_closure | high | Site closure |
| production_interruption | medium | Production halt |
| contract_loss | medium | Major contract loss |
| contract_win | low | Major contract win (positive) |
| regulatory_action | high | Fine, sanction, enforcement |
| sanction | critical | Sanctions listing |
| recall | medium | Product recall |
| litigation_material | medium | Material litigation |
| cyber_incident | high | Cyber attack/breach |
| environmental_incident | medium | Environmental event |
| logistics_incident | medium | Supply chain disruption |
| quality_incident | medium | Quality failure |
| unconfirmed_adverse | low | Credible but unconfirmed adverse report |
