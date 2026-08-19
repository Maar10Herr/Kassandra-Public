"""Evidence collector — runs all active source adapters with observability.

Discovers new evidence for portfolio companies from:
- Companies House (UK filing history)
- UK Gazette (insolvency notices)
- Web monitor (sitemaps, RSS feeds, conditional GET)
- GLEIF (LEI data as evidence, not event source)
- Handelsregister (German commercial register)
- BODACC (French commercial announcements)
- BORME (Spanish commercial register)

All evidence is stored immutably with provenance.
Every run is tracked with run_id, source health, and journal.
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from kassandra.config import get_config
from kassandra.contracts import CollectionMetrics
from kassandra.evidence import store_evidence, store_event
from kassandra.observability import (
    finish_run,
    log_source_event,
    start_run,
)
from kassandra.sources.companies_house import CompaniesHouseClient
from kassandra.sources.gazette import GazetteClient
from kassandra.sources.gleif import GleifClient
from kassandra.sources.bodacc import BodaccClient
from kassandra.sources.handelsregister import HandelsregisterClient
from kassandra.sources.borme import BorMeClient
from kassandra.sources.web_monitor import WebMonitor

logger = logging.getLogger(__name__)


def _classify_collection_error(exc: Exception) -> str:
    """Map arbitrary adapter failures to a bounded operational category."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
        return "timeout"
    if isinstance(exc, PermissionError) or any(token in text for token in ("401", "403", "unauthorized", "forbidden", "auth")):
        return "auth"
    if isinstance(exc, (ValueError, json.JSONDecodeError)) or any(token in text for token in ("parse", "invalid json", "decode")):
        return "parse"
    if any(token in text for token in ("blocked", "captcha", "rate limit", "429")):
        return "block"
    if any(token in text for token in ("unavailable", "unreachable", "connection", "dns")):
        return "unavailable"
    return "unknown"

EVENT_SEVERITY = {
    "insolvency": "critical",
    "restructuring": "high",
    "going_concern_warning": "high",
    "auditor_warning": "high",
    "payment_stress": "high",
    "refinancing_stress": "high",
    "emergency_capital": "high",
    "profit_warning": "medium",
    "guidance_withdrawal": "medium",
    "late_reporting": "medium",
    "revised_reporting": "medium",
    "management_departure": "low",
    "auditor_departure": "medium",
    "layoffs": "medium",
    "hiring_freeze": "low",
    "facility_closure": "high",
    "production_interruption": "medium",
    "contract_loss": "medium",
    "contract_win": "low",
    "regulatory_action": "high",
    "sanction": "critical",
    "recall": "medium",
    "litigation_material": "medium",
    "cyber_incident": "high",
    "environmental_incident": "medium",
    "logistics_incident": "medium",
    "quality_incident": "medium",
    "unconfirmed_adverse": "low",
}


def run_collection(db: sqlite3.Connection, sources: list[str] | None = None) -> dict[str, CollectionMetrics]:
    """Run active source adapters with full observability.

    Args:
        db: Database connection.
        sources: If provided, only run these named sources. Otherwise run all.
                 Valid names: 'companies_house', 'uk_gazette', 'web_monitor',
                 'handelsregister_de', 'bodacc_fr', 'borme_es'.

    Returns dict of source_name → CollectionMetrics.
    """
    if sources is None:
        sources = ["companies_house", "uk_gazette", "web_monitor", "handelsregister_de", "bodacc_fr", "borme_es"]
    run_id = start_run(db, "collection")
    metrics: dict[str, CollectionMetrics] = {}
    latencies: dict[str, float] = {}
    now = datetime.now(timezone.utc).isoformat()

    # 1. Register all active sources
    _register_sources(db)

    # Helper: run a source collector, catching failures
    def _run_source(name: str, collector_fn, *args):
        t0 = time.monotonic()
        try:
            m = collector_fn(db, run_id, *args)
            latencies[name] = time.monotonic() - t0
            m.assert_reconciled()
            metrics[name] = m
            if m.new_evidence > 0:
                log_source_event(db, name, "api",
                                 _source_base_url(name),
                                 "evidence_collected", {"count": m.new_evidence})
            else:
                log_source_event(db, name, "api",
                                 _source_base_url(name), "success")
        except Exception as e:
            latencies[name] = time.monotonic() - t0
            error_type = _classify_collection_error(e)
            logger.error(f"{name} collection failed: {e}")
            log_source_event(db, name, "api",
                             _source_base_url(name),
                             "failure", {"error": str(e), "error_type": error_type})
            metrics[name] = CollectionMetrics(
                run_id=run_id, source_name=name,
                errors=1,
                api_errors=0 if error_type == "parse" else 1,
                parse_failures=1 if error_type == "parse" else 0,
                error_type=error_type,
                error_message=str(e)[:500],
            )

    # 2. Companies House filing history
    if "companies_house" in sources:
        _run_source("companies_house", _collect_companies_house)

    # 3. UK Gazette insolvency notices
    if "uk_gazette" in sources:
        _run_source("uk_gazette", _collect_gazette)

    # 4. Handelsregister (German commercial register)
    if "handelsregister_de" in sources:
        _run_source("handelsregister_de", _collect_handelsregister)

    # 5. Web monitoring (sitemaps, feeds, pages)
    if "web_monitor" in sources:
        _run_source("web_monitor", _collect_web_monitor)

    # 6. BODACC (French commercial announcements)
    if "bodacc_fr" in sources:
        _run_source("bodacc_fr", _collect_bodacc)

    # 7. BORME (Spanish commercial register announcements)
    if "borme_es" in sources:
        _run_source("borme_es", _collect_borme)

    # 8. GLEIF is queried during graph build, not collection — but register source
    log_source_event(db, "gleif", "api",
                     "https://api.gleif.org/api/v1", "success")

    db.commit()

    total_new = sum(m.new_evidence for m in metrics.values())
    total_events = sum(m.events_created for m in metrics.values())
    logger.info(f"Collection complete: {total_new} new evidence items, {total_events} events")

    # Finish run with summary
    finish_run(db, run_id, "collection", summary={
        "results": {k: v.to_dict() for k, v in metrics.items()},
        "total_new_evidence": total_new,
        "total_events": total_events,
        "completed_at": now,
    })

    # Record per-source yield with reconciliation fields
    _record_source_yields(db, run_id, metrics, latencies)

    return metrics


def _source_base_url(name: str) -> str | None:
    """Get the base URL for a source."""
    urls = {
        "companies_house": "https://api.companieshouse.gov.uk",
        "uk_gazette": "https://www.thegazette.co.uk",
        "web_monitor": None,
        "handelsregister_de": "https://www.handelsregister.de",
        "bodacc_fr": "https://www.bodacc.fr",
        "borme_es": "https://www.boe.es",
    }
    return urls.get(name)


def _register_sources(db: sqlite3.Connection) -> None:
    """Ensure all active sources are registered in the sources table."""
    sources = [
        ("companies_house", "api", "https://api.companieshouse.gov.uk"),
        ("gleif", "api", "https://api.gleif.org/api/v1"),
        ("uk_gazette", "feed", "https://www.thegazette.co.uk"),
        ("web_monitor", "web", None),
        ("handelsregister_de", "api", "https://www.handelsregister.de"),
        ("bodacc_fr", "api", "https://www.bodacc.fr"),
        ("borme_es", "feed", "https://www.boe.es"),
        ("known_feeds", "feed", None),
        ("content_classifier", "classifier", None),
        ("graph_builder", "processor", None),
        ("scorer", "processor", None),
    ]
    for name, stype, url in sources:
        db.execute(
            """INSERT OR IGNORE INTO sources
               (source_name, source_type, base_url, status)
               VALUES (?, ?, ?, 'active')""",
            (name, stype, url),
        )
    db.commit()
    logger.info(f"Registered {len(sources)} sources")


def _collect_companies_house(db: sqlite3.Connection, run_id: str) -> CollectionMetrics:
    """Collect filing history for UK-registered portfolio companies.

    Returns CollectionMetrics with evidence and event reconciliation.
    """
    ch_client = CompaniesHouseClient()
    if not ch_client.available:
        logger.info("Companies House not available, skipping")
        log_source_event(db, "companies_house", "api",
                         "https://api.companieshouse.gov.uk",
                         "failure", {"error": "not_available"})
        return CollectionMetrics(
            run_id=run_id, source_name="companies_house", errors=1,
            api_errors=1, error_type="auth",
            error_message="Companies House API key unavailable",
        )

    rows = db.execute(
        """SELECT r.id, r.canonical_name, r.companies_house_number
           FROM registry r
           WHERE r.companies_house_number IS NOT NULL"""
    ).fetchall()

    discovered = 0
    new_evidence = 0
    duplicates = 0
    candidates = 0
    events_created = 0
    duplicate_events = 0
    errors = 0
    api_errors = 0
    parse_failures = 0
    last_error_type: str | None = None
    last_error_message: str | None = None
    now = datetime.now(timezone.utc).isoformat()

    for row in rows:
        try:
            filing_history = ch_client.get_filing_history(
                row["companies_house_number"], items_per_page=10)
            if not filing_history or "items" not in filing_history:
                if ch_client.last_error is not None:
                    errors += 1
                    last_error_type = _classify_collection_error(ch_client.last_error)
                    last_error_message = str(ch_client.last_error)[:500]
                    if last_error_type == "parse":
                        parse_failures += 1
                    else:
                        api_errors += 1
                continue

            for item in filing_history["items"]:
                discovered += 1
                filing_json = json.dumps(item)
                ev_result = store_evidence(
                    db=db, content=filing_json,
                    source_url=(
                        f"https://api.companieshouse.gov.uk/company/"
                        f"{row['companies_house_number']}/filing-history"
                    ),
                    retrieval_time=now, publication_time=item.get("date"),
                    extraction_method="companies_house_api", parser_version="1.0.0",
                    content_type="application/json",
                    excerpt=item.get("description", ""),
                    source_reliability=1.0,
                )
                if ev_result.is_new:
                    new_evidence += 1
                else:
                    duplicates += 1

                evidence_id = ev_result.evidence_id

                event_type, severity = _classify_filing(item)
                if event_type:
                    candidates += 1
                    evt_result = store_event(
                        db=db, evidence_id=evidence_id, registry_id=row["id"],
                        event_type=event_type, severity=severity,
                        confidence=1.0, description=item.get("description", ""),
                        source_claims_directly=True, raw_event_json=filing_json,
                    )
                    if evt_result.status == "inserted":
                        events_created += 1
                    elif evt_result.status == "duplicate":
                        duplicate_events += 1
        except Exception as e:
            logger.warning(f"Companies House failed for {row['canonical_name']}: {e}")
            errors += 1
            last_error_type = _classify_collection_error(e)
            last_error_message = str(e)[:500]
            if last_error_type == "parse":
                parse_failures += 1
            else:
                api_errors += 1

    return CollectionMetrics(
        run_id=run_id, source_name="companies_house",
        discovered=discovered,
        fetched=new_evidence + duplicates,
        new_evidence=new_evidence, duplicates=duplicates,
        candidates=candidates,
        events_created=events_created, duplicate_events=duplicate_events,
        errors=errors, api_errors=api_errors, parse_failures=parse_failures,
        error_type=last_error_type, error_message=last_error_message,
    )


def _collect_gazette(db: sqlite3.Connection, run_id: str) -> CollectionMetrics:
    """Collect UK Gazette insolvency notices for portfolio companies.

    Fetches the feed ONCE, then matches against all UK companies.
    Now returns CollectionMetrics with real event counts (P1 fix: was always 0).
    """
    gazette = GazetteClient()

    # Fetch feed once for all companies
    notices = gazette.fetch_insolvency_feed(max_entries=50)
    if not notices:
        logger.warning("Gazette: no notices fetched (feed may be empty or rate-limited)")
        if gazette.last_error is not None:
            error_type = _classify_collection_error(gazette.last_error)
            return CollectionMetrics(
                run_id=run_id, source_name="uk_gazette", errors=1,
                api_errors=0 if error_type == "parse" else 1,
                parse_failures=1 if error_type == "parse" else 0,
                error_type=error_type, error_message=str(gazette.last_error)[:500],
            )
        return CollectionMetrics(run_id=run_id, source_name="uk_gazette")

    rows = db.execute(
        """SELECT r.id, r.canonical_name
           FROM registry r
           WHERE r.jurisdiction IN ('GB', 'UK', 'JE', 'GG',
                                     'england-wales', 'scotland',
                                     'northern-ireland')
              OR r.companies_house_number IS NOT NULL"""
    ).fetchall()

    new_evidence = 0
    duplicates = 0
    events_created = 0
    duplicate_events = 0
    candidates = 0
    unconfirmed = 0
    discovered = len(notices)

    for row in rows:
        outcome = gazette.collect_for_company_from_notices(
            db=db, registry_id=row["id"], company_name=row["canonical_name"], notices=notices,
        )
        new_evidence += outcome.new_evidence
        duplicates += outcome.duplicates
        events_created += outcome.events_created
        duplicate_events += outcome.duplicate_events
        candidates += outcome.candidates
        unconfirmed += outcome.unconfirmed

    return CollectionMetrics(
        run_id=run_id, source_name="uk_gazette",
        discovered=max(discovered, new_evidence + duplicates),
        fetched=new_evidence + duplicates,
        new_evidence=new_evidence, duplicates=duplicates,
        candidates=candidates, unconfirmed=unconfirmed,
        events_created=events_created, duplicate_events=duplicate_events,
        errors=0,
    )


def _collect_handelsregister(db: sqlite3.Connection, run_id: str) -> CollectionMetrics:
    """Collect German Handelsregister data for DE-jurisdiction portfolio companies.

    Returns CollectionMetrics with truthful reconciled counters.
    """
    try:
        hr_client = HandelsregisterClient()
    except Exception as e:
        logger.info(f"Handelsregister not available: {e}")
        return CollectionMetrics(run_id=run_id, source_name="handelsregister_de", errors=1)

    rows = db.execute(
        """SELECT r.id, r.canonical_name
           FROM registry r
           WHERE r.jurisdiction IN ('DE')"""
    ).fetchall()

    discovered = 0
    new_evidence = 0
    duplicates = 0
    candidates = 0
    events_created = 0
    duplicate_events = 0
    errors = 0

    for row in rows:
        try:
            outcome = hr_client.collect_for_company_metrics(
                db=db, registry_id=row["id"],
                company_name=row["canonical_name"],
                run_id=run_id,
            )
            discovered += outcome.discovered
            new_evidence += outcome.new_evidence
            duplicates += outcome.duplicates
            candidates += outcome.candidates
            events_created += outcome.events_created
            duplicate_events += outcome.duplicate_events
        except Exception as e:
            logger.warning(f"Handelsregister failed for {row['canonical_name']}: {e}")
            errors += 1

    return CollectionMetrics(
        run_id=run_id, source_name="handelsregister_de",
        discovered=discovered,
        fetched=new_evidence + duplicates,
        new_evidence=new_evidence, duplicates=duplicates,
        candidates=candidates,
        events_created=events_created, duplicate_events=duplicate_events,
        errors=errors,
    )


def _collect_web_monitor(db: sqlite3.Connection, run_id: str, use_async: bool = True) -> CollectionMetrics:
    """Monitor company websites and feeds, using bounded parallel HTTP by default.

    Returns CollectionMetrics.
    """
    if use_async:
        return _collect_web_monitor_async(db, run_id)

    monitor = WebMonitor()
    attempted_evidence = 0
    errors = 0
    evidence_before = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]

    rows = db.execute(
        """SELECT r.id, r.canonical_name, r.domain, r.ir_url, r.feed_url
           FROM registry r
           WHERE r.domain IS NOT NULL
           ORDER BY r.id"""
    ).fetchall()

    for row in rows:
        try:
            count = monitor.monitor_company(
                db=db,
                registry_id=row["id"],
                domain=row["domain"],
                ir_url=row["ir_url"],
                feed_url=row["feed_url"],
            )
            attempted_evidence += count
            if count > 0:
                logger.info(
                    f"  {row['canonical_name']}: {count} web evidence items")
        except Exception as e:
            logger.warning(
                f"Web monitor failed for {row['canonical_name']}: {e}")
            errors += 1

    # The monitor reports successful store attempts; derive inserted evidence from
    # the actual evidence-table delta so replayed content is never called new.
    new_evidence = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] - evidence_before
    duplicates = attempted_evidence - new_evidence
    return CollectionMetrics(
        run_id=run_id, source_name="web_monitor",
        discovered=len(rows), fetched=attempted_evidence,
        new_evidence=new_evidence, duplicates=duplicates,
        candidates=0, events_created=0, duplicate_events=0,
        errors=errors,
    )


def _collect_web_monitor_async(db: sqlite3.Connection, run_id: str) -> CollectionMetrics:
    """Monitor all companies in parallel using async HTTP.

    Returns CollectionMetrics.
    """
    import asyncio
    from kassandra.sources.async_web_monitor import AsyncWebMonitor

    rows = db.execute(
        """SELECT r.id as registry_id, r.canonical_name, r.domain, r.ir_url, r.feed_url
           FROM registry r
           WHERE r.domain IS NOT NULL
           ORDER BY r.id"""
    ).fetchall()

    companies = [dict(r) for r in rows]
    monitor = AsyncWebMonitor()
    evidence_before = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]

    counts = asyncio.run(monitor.monitor_all_companies(db, companies))
    attempted_evidence = sum(counts.values())
    new_evidence = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] - evidence_before
    duplicates = attempted_evidence - new_evidence

    for c in companies:
        cnt = counts.get(c["registry_id"], 0)
        if cnt > 0:
            logger.info(f"  {c['canonical_name'][:40]}: {cnt} web evidence items")

    return CollectionMetrics(
        run_id=run_id, source_name="web_monitor",
        discovered=len(companies),
        fetched=attempted_evidence,
        new_evidence=new_evidence, duplicates=duplicates,
        candidates=0, events_created=0, duplicate_events=0,
        errors=0,
    )


def _collect_bodacc(db: sqlite3.Connection, run_id: str, days_back: int = 30) -> CollectionMetrics:
    """Collect French BODACC announcements for portfolio companies.

    Fetches notices ONCE for all monitored families, then matches against
    all French-jurisdiction companies.

    Returns CollectionMetrics.
    """
    bodacc = BodaccClient()

    # Fetch notices once across all monitored families
    notices = bodacc.fetch_notices(days_back=days_back)
    if not notices:
        logger.warning("BODACC: no notices fetched (API may be empty or unreachable)")
        return CollectionMetrics(run_id=run_id, source_name="bodacc_fr")

    rows = db.execute(
        """SELECT r.id, r.canonical_name
           FROM registry r
           WHERE r.jurisdiction IN ('FR')"""
    ).fetchall()

    new_evidence = 0
    duplicates = 0
    events_created = 0
    duplicate_events = 0
    candidates = 0
    unconfirmed = 0

    for row in rows:
        matches = bodacc.filter_notices_for_company(
            row["canonical_name"], notices
        )
        if matches:
            outcome = bodacc.process_notices_for_company(
                db=db, registry_id=row["id"], company_name=row["canonical_name"], notices=matches,
            )
            new_evidence += outcome.new_evidence
            duplicates += outcome.duplicates
            candidates += outcome.candidates
            unconfirmed += outcome.unconfirmed
            events_created += outcome.events_created
            duplicate_events += outcome.duplicate_events

    return CollectionMetrics(
        run_id=run_id, source_name="bodacc_fr",
        # A notice can be evaluated against several registry candidates.
        discovered=max(len(notices), new_evidence + duplicates),
        fetched=new_evidence + duplicates,
        new_evidence=new_evidence, duplicates=duplicates,
        candidates=candidates, unconfirmed=unconfirmed,
        events_created=events_created, duplicate_events=duplicate_events,
        errors=0,
    )


def _collect_borme(db: sqlite3.Connection, run_id: str) -> CollectionMetrics:
    """Collect Spain BORME announcements for portfolio companies.

    Fetches the RSS feed ONCE, then matches against all Spanish-jurisdiction companies.

    Returns CollectionMetrics.
    """
    borme = BorMeClient()

    # Fetch feed once for all companies
    entries = borme.fetch_feed(max_entries=100)
    if not entries:
        logger.warning("BORME: no entries fetched (feed may be empty or unreachable)")
        return CollectionMetrics(run_id=run_id, source_name="borme_es")

    rows = db.execute(
        """SELECT r.id, r.canonical_name
           FROM registry r
           WHERE r.jurisdiction IN ('ES')"""
    ).fetchall()

    new_evidence = 0
    duplicates = 0
    events_created = 0
    duplicate_events = 0
    candidates = 0
    unconfirmed = 0

    for row in rows:
        matches = borme.filter_notices_for_company(
            row["canonical_name"], entries
        )
        if matches:
            outcome = borme.process_notices_for_company(
                db=db, registry_id=row["id"], company_name=row["canonical_name"], notices=matches,
            )
            new_evidence += outcome.new_evidence
            duplicates += outcome.duplicates
            candidates += outcome.candidates
            unconfirmed += outcome.unconfirmed
            events_created += outcome.events_created
            duplicate_events += outcome.duplicate_events

    return CollectionMetrics(
        run_id=run_id, source_name="borme_es",
        discovered=max(len(entries), new_evidence + duplicates),
        fetched=new_evidence + duplicates,
        new_evidence=new_evidence, duplicates=duplicates,
        candidates=candidates, unconfirmed=unconfirmed,
        events_created=events_created, duplicate_events=duplicate_events,
        errors=0,
    )


def _classify_filing(filing: dict) -> tuple[str | None, str | None]:
    """Classify a Companies House filing into an event type."""
    description = filing.get("description", "").lower()
    category = filing.get("category", "").lower()

    if any(term in description for term in [
        "insolvency", "winding-up", "liquidation", "administration",
        "moratorium", "receiver", "voluntary arrangement",
    ]):
        return "insolvency", "critical"

    if any(term in description for term in [
        "auditor", "auditors", "going concern",
        "qualified report", "resignation of auditor",
    ]):
        if "resignation" in description or "removed" in description:
            return "auditor_departure", "medium"
        return "auditor_warning", "high"

    if any(term in description for term in [
        "capital reduction", "solvency statement", "statement of capital",
        "resolution", "restructuring",
    ]):
        return "restructuring", "high" if "reduction" in description else "medium"

    if "accounts" in category and "overdue" in description:
        return "late_reporting", "medium"

    if "officers" in category:
        if "termination" in description or "resignation" in description:
            return "management_departure", "low"

    return None, None


def _record_source_yields(
    db: sqlite3.Connection, run_id: str, metrics: dict[str, CollectionMetrics],
    latencies: dict[str, float] | None = None,
) -> None:
    """Record per-source yield in source_runs with reconciliation fields.

    Uses the canonical CollectionMetrics to populate all reconciliation
    columns: documents_discovered, documents_fetched, new_documents,
    duplicate_documents, candidates_generated, unconfirmed_matches,
    events_created, duplicate_events, errors.
    """
    now = datetime.now(timezone.utc).isoformat()
    latencies = latencies or {}
    source_run_columns = {row[1] for row in db.execute("PRAGMA table_info(source_runs)")}
    has_status = "status" in source_run_columns
    has_error_detail = "error_detail_json" in source_run_columns
    for source_name, m in metrics.items():
        status_columns = ", status" if has_status else ""
        status_values = ", ?" if has_status else ""
        error_columns = ", error_detail_json" if has_error_detail else ""
        error_values = ", ?" if has_error_detail else ""
        values = [run_id, source_name,
                  m.discovered, m.fetched,
                  m.new_evidence, m.duplicates,
                  m.candidates, m.unconfirmed,
                  m.events_created, m.duplicate_events,
                  m.errors, m.api_errors, m.parse_failures,
                  m.events_created,
                  latencies.get(source_name)]
        if has_status:
            values.append("failed" if m.errors else "success")
        if has_error_detail:
            values.append(
                json.dumps({"type": m.error_type, "message": m.error_message})
                if m.error_type else None
            )
        values.extend([now, now])
        db.execute(
            f"""INSERT INTO source_runs
               (run_id, source_name,
                documents_discovered, documents_fetched,
                new_documents, duplicate_documents,
                candidates_generated, unconfirmed_matches,
                events_created, duplicate_events,
                errors, api_errors, parse_failures,
                adverse_events_found,
                latency_seconds{status_columns}{error_columns},
                started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?{status_values}{error_values}, ?, ?)""",
            values,
        )

        # Classifier yield is post-identity-gate truth: candidates routed to the
        # review queue are not accepted events, and duplicate event insertions
        # are reported separately rather than inflated as new accepted events.
        from kassandra.classifier import record_classifier_run
        record_classifier_run(db, run_id, source_name, None, {
            "documents_discovered": m.discovered,
            "documents_fetched": m.fetched,
            "documents_with_text": m.fetched,
            "documents_classified": m.fetched,
            "candidate_pattern_hits": m.candidates,
            "rejected_candidates": max(
                m.candidates - m.unconfirmed - m.events_created - m.duplicate_events, 0
            ),
            "accepted_events": m.events_created,
            "false_positive_checks": m.unconfirmed,
        })
    db.commit()
