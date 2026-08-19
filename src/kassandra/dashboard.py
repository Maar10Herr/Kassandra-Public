"""Lightweight local dashboard — HTML server with evidence-backed views.

Per engineering audit P9: shows why companies are prioritized,
source freshness, adverse events, evidence links, graph paths,
confidence, unknowns, and score changes.
"""

import http.server
import json
import logging
import sqlite3
import urllib.parse

from kassandra.db import get_db

logger = logging.getLogger(__name__)


def run_dashboard(port: int = 8765) -> None:
    """Start the local dashboard HTTP server."""
    db = get_db()
    server = http.server.HTTPServer(
        ("127.0.0.1", port),
        lambda *args: DashboardHandler(db, *args),
    )
    logger.info(f"Dashboard listening on http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


class DashboardHandler(http.server.BaseHTTPRequestHandler):

    def __init__(self, db: sqlite3.Connection, *args, **kwargs):
        self.db = db
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_html(self._index_html())
        elif path == "/api/companies":
            self._serve_json(self._get_companies())
        elif path == "/api/company":
            params = urllib.parse.parse_qs(parsed.query)
            reg_id = params.get("id", [None])[0]
            self._serve_json(
                self._get_company_detail(int(reg_id)) if reg_id
                else {"error": "missing id"}, 400 if not reg_id else 200
            )
        elif path == "/api/scores":
            self._serve_json(self._get_scores())
        elif path == "/api/events":
            params = urllib.parse.parse_qs(parsed.query)
            reg_id = params.get("registry_id", [None])[0]
            self._serve_json(self._get_events(reg_id))
        elif path == "/api/sources":
            self._serve_json(self._get_sources())
        elif path == "/api/graph":
            params = urllib.parse.parse_qs(parsed.query)
            reg_id = params.get("id", [None])[0]
            self._serve_json(self._get_graph(reg_id))
        elif path == "/api/observability":
            self._serve_json(self._get_observability())
        elif path == "/api/coverage":
            self._serve_json(self._get_coverage())
        else:
            self._serve_json({"error": "not found"}, 404)

    def _serve_html(self, html: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_json(self, data: object, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode("utf-8"))

    def _index_html(self) -> str:
        return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kassandra — Investigation Dashboard</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 1rem;
         background: #1a1a2e; color: #e0e0e0; }
  h1 { color: #00d4aa; margin: 0 0 0.25rem; }
  h2 { color: #00d4aa; margin: 0.5rem 0; }
  table { width: 100%; border-collapse: collapse; margin: 0.5rem 0; font-size: 0.85rem; }
  th, td { padding: 0.4rem 0.5rem; text-align: left; border-bottom: 1px solid #333; }
  th { color: #00d4aa; position: sticky; top: 0; background: #1a1a2e; }
  tr:hover { background: #16213e; }
  .nav { margin: 0.75rem 0; display: flex; gap: 0.5rem; flex-wrap: wrap; }
  .nav a, .nav button { color: #00d4aa; text-decoration: none; cursor: pointer;
      background: none; border: 1px solid #00d4aa; padding: 0.3rem 0.7rem;
      border-radius: 4px; font-size: 0.8rem; }
  .nav a:hover, .nav button:hover { background: #00d4aa22; }
  .nav .active { background: #00d4aa44; }
  .bar { height: 0.8rem; background: #00d4aa; border-radius: 4px; }
  .bar-bg { background: #333; border-radius: 4px; }
  .critical { color: #ff4444; font-weight: bold; }
  .high { color: #ff8800; font-weight: bold; }
  .medium { color: #ffcc00; }
  .low { color: #888; }
  .tag { display: inline-block; padding: 2px 6px; margin: 1px;
         border-radius: 3px; font-size: 0.75rem; }
  .tag-critical { background: #ff4444; color: #fff; }
  .tag-high { background: #ff8800; color: #fff; }
  .tag-medium { background: #cca300; color: #000; }
  .tag-low { background: #555; color: #ccc; }
  .tag-unknown { background: #444; color: #888; font-style: italic; }
  .card { background: #16213e; border-radius: 8px; padding: 0.75rem; margin: 0.5rem 0; }
  .stat { display: inline-block; margin: 0.25rem 1rem 0.25rem 0; }
  .stat-val { font-size: 1.2rem; color: #00d4aa; font-weight: bold; }
  .stat-label { font-size: 0.7rem; color: #888; }
  .explanation { font-size: 0.8rem; color: #aaa; margin: 0.25rem 0; font-style: italic; }
  details { margin: 0.25rem 0; }
  summary { cursor: pointer; color: #00d4aa; font-size: 0.85rem; }
  #content { margin-top: 0.5rem; }
  .source-health { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  .source-card { background: #16213e; padding: 0.5rem 0.75rem; border-radius: 6px;
      min-width: 180px; font-size: 0.8rem; }
  .source-card.active { border-left: 3px solid #00d4aa; }
  .source-card.degraded { border-left: 3px solid #ff8800; }
  .source-card.disabled { border-left: 3px solid #ff4444; }
</style>
</head>
<body>
<h1>⚕ Kassandra</h1>
<p style="color:#888;font-size:0.85rem;margin:0">
  Corporate early-warning — investigation dashboard | 50 portfolio companies | v0.3.0-dev
</p>
<div class="nav">
  <button onclick="loadCompanies()" id="nav-companies">Companies</button>
  <button onclick="loadScores()" id="nav-scores">Scores</button>
  <button onclick="loadSources()" id="nav-sources">Source Health</button>
  <button onclick="loadObservability()" id="nav-obs">Observability</button>
  <button onclick="loadCoverage()" id="nav-cov">Coverage</button>
</div>
<div id="content"><p>Loading...</p></div>
<script>
function setActive(id) {
  document.querySelectorAll('.nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(id)?.classList.add('active');
}

async function loadCompanies() {
  setActive('nav-companies');
  const resp = await fetch('/api/companies');
  const data = await resp.json();
  let html = `<h2>Portfolio Companies</h2>
    <table><tr><th>Company</th><th>Events</th><th>Dependencies</th><th>Mat Known?</th><th>Priority</th></tr>`;
  for (const c of data) {
    const sev = c.max_severity || 'none';
    const matLabel = c.materiality_known ? '' : ' <span class="tag tag-unknown">defaults</span>';
    html += `<tr>
      <td><a href="javascript:showCompany(${c.id})">${esc(c.canonical_name)}</a></td>
      <td>${c.event_count}</td>
      <td>${c.edge_count || '-'}</td>
      <td>${c.materiality_known ? '✓' : matLabel}</td>
      <td><span class="${sev}">${c.analyst_priority?.toFixed(3) || '-'}</span></td>
    </tr>`;
  }
  html += '</table>';
  document.getElementById('content').innerHTML = html;
}

async function loadScores() {
  setActive('nav-scores');
  const resp = await fetch('/api/scores');
  const data = await resp.json();
  let html = `<h2>Investigation Scores (Heuristic Triage Only)</h2>
    <p class="explanation">⚠ Scores decompose into signal, recency, credibility, legal ownership exposure, and materiality. <em>All materiality is unknown — no economic data exists.</em> <strong>These are NOT risk scores — all weights are hand-chosen heuristics.</strong></p>
    <table>
    <tr><th>Company</th><th>Events</th><th>Signal</th><th>Exposure</th><th>Materiality</th><th>Priority</th><th>Explanation</th></tr>`;
  for (const s of data) {
    const matNote = s.materiality_known ? s.materiality_score?.toFixed(2) : '<span class="tag tag-unknown">unknown</span>';
    html += `<tr>
      <td>${esc(s.canonical_name)}</td>
      <td>${s.event_count}</td>
      <td>${s.signal_score?.toFixed(3) || '0'}</td>
      <td>${s.legal_ownership_exposure?.toFixed(0) || '0'}</td>
      <td>${matNote}</td>
      <td>
        <div class="bar-bg"><div class="bar" style="width:${(s.analyst_priority||0)*100}%"></div></div>
        ${(s.analyst_priority||0).toFixed(3)}
      </td>
      <td style="font-size:0.75rem;max-width:300px">${esc(s.explanation || '-')}</td>
    </tr>`;
  }
  html += '</table>';
  document.getElementById('content').innerHTML = html;
}

async function showCompany(id) {
  setActive('nav-companies');
  const [cResp, gResp] = await Promise.all([
    fetch('/api/company?id=' + id),
    fetch('/api/graph?id=' + id)
  ]);
  const c = await cResp.json();
  const g = await gResp.json();
  if (c.error) { document.getElementById('content').innerHTML = '<p>Not found</p>'; return; }
  let html = `<h2>${esc(c.canonical_name)}</h2>
    <div class="card">
      <span class="stat"><span class="stat-label">Events</span><br><span class="stat-val">${c.events?.length || 0}</span></span>
      <span class="stat"><span class="stat-label">Legal Ownership Edges</span><br><span class="stat-val">${g.edge_count || 0}${g.graph_capped ? ' ⚠' : ''}</span></span>
      <span class="stat"><span class="stat-label">Exposure</span><br><span class="stat-val">${g.exposure || 0}</span></span>
      <span class="stat"><span class="stat-label">Materiality</span><br><span class="stat-val">${g.materiality_known ? 'known' : '<span class="tag tag-unknown">unknown</span>'}</span></span>
    </div>`;

  // Events
  html += `<h3>Events (${c.events?.length || 0})</h3>`;
  if (c.events?.length) {
    for (const e of c.events) {
      html += `<div class="card">
        <span class="tag tag-${e.severity || 'low'}">${e.severity}</span>
        <strong>${e.event_type}</strong> — ${esc(e.description || '')}
        <br><small>${e.extracted_at} | conf: ${e.confidence?.toFixed(2)}</small>
      </div>`;
    }
  } else {
    html += '<p style="color:#888">No events detected. This is expected for currently healthy companies.</p>';
  }

  // Legal ownership graph + economic dependencies
  const ecoEdges = g.economic_edges || [];
  const corrCount = g.legal_edges?.filter(e => e.validation === 'annual_report_corroborated').length || 0;
  html += `<h3>Dependency Graph</h3>
    <div class="card">
      <span class="stat"><span class="stat-label">Legal Edges (GLEIF)</span><br><span class="stat-val">${g.edge_count || 0}${g.graph_capped ? ' ⚠' : ''}</span></span>
      <span class="stat"><span class="stat-label">Corroborated</span><br><span class="stat-val">${corrCount}</span></span>
      <span class="stat"><span class="stat-label">Economic Edges</span><br><span class="stat-val">${ecoEdges.length}</span></span>
      <span class="stat"><span class="stat-label">Materiality</span><br><span class="stat-val">${g.materiality_known ? 'estimated' : '<span class=\"tag tag-unknown\">none</span>'}</span></span>
    </div>`;

  // Economic dependencies
  if (ecoEdges.length) {
    html += `<h4>Economic Dependencies (${ecoEdges.length})</h4>
      <p class="explanation">Automated extraction from annual reports. Customer, supplier, facility, commodity, operational.</p>
      <table><tr><th>Entity</th><th>Type</th><th>Evidence</th></tr>`;
    for (const e of ecoEdges.slice(0, 20)) {
      html += `<tr>
        <td>${esc(e.name)}</td>
        <td><span class="tag tag-${e.type === 'supplier' ? 'high' : 'medium'}">${e.type}</span></td>
        <td style="font-size:0.7rem;max-width:300px">${esc(e.description?.substring(0, 100) || '')}</td>
      </tr>`;
    }
    html += '</table>';
  }

  // Legal ownership edges
  html += `<h4>Legal Ownership Edges (${g.edge_count || 0})</h4>
    <p class="explanation">GLEIF parent/subsidiary relationships. Materiality is proxy-estimated from corroboration level.</p>`;
  if (g.legal_edges?.length) {
    html += '<table><tr><th>Entity</th><th>Type</th><th>Confidence</th><th>Materiality</th></tr>';
    for (const e of g.legal_edges.slice(0, 30)) {
      html += `<tr>
        <td>${esc(e.target)}</td>
        <td>${e.type}</td>
        <td>${e.confidence?.toFixed(2) || '?'}</td>
        <td>${e.materiality != null ? e.materiality.toFixed(2) : '<span class="tag tag-unknown">unknown</span>'}</td>
      </tr>`;
    }
    if (g.edges.length > 30) html += `<tr><td colspan="4" style="color:#888">... and ${g.edges.length - 30} more</td></tr>`;
    html += '</table>';
    html += `<p class="explanation">${esc(g.explanation || '')}</p>`;
  } else {
    html += '<p style="color:#888">No legal ownership data.</p>';
  }

  html += '<p><a href="javascript:loadCompanies()">← Back</a></p>';
  document.getElementById('content').innerHTML = html;
}

async function loadSources() {
  setActive('nav-sources');
  const resp = await fetch('/api/sources');
  const data = await resp.json();
  let html = `<h2>Source Health</h2><div class="source-health">`;
  for (const s of data) {
    const cls = s.status === 'active' ? 'active' : (s.status === 'degraded' ? 'degraded' : 'disabled');
    const lastSuccess = s.last_success_at ? s.last_success_at.substring(0, 16) : 'never';
    html += `<div class="source-card ${cls}">
      <strong>${s.source_name}</strong>
      <div>${s.source_type} · ${s.status}</div>
      <div>Evidence: ${s.total_evidence} | Failures: ${s.consecutive_failures}</div>
      <div style="font-size:0.7rem;color:#888">Last ok: ${lastSuccess}</div>
    </div>`;
  }
  html += '</div>';
  document.getElementById('content').innerHTML = html;
}

async function loadObservability() {
  setActive('nav-obs');
  const resp = await fetch('/api/observability');
  const data = await resp.json();
  let html = `<h2>Observability</h2>`;

  if (data.source_health?.length) {
    html += `<h3>Source Status</h3><table>
      <tr><th>Source</th><th>Status</th><th>Evidence</th><th>Events</th><th>Last Success</th><th>Failures</th></tr>`;
    for (const s of data.source_health) {
      const last = s.last_success_at ? s.last_success_at.substring(0, 16) : 'never';
      html += `<tr>
        <td>${s.source_name}</td>
        <td class="${s.status === 'active' ? '' : 'critical'}">${s.status}</td>
        <td>${s.total_evidence}</td>
        <td>${s.unique_events}</td>
        <td>${last}</td>
        <td>${s.consecutive_failures}</td>
      </tr>`;
    }
    html += '</table>';
  }

  if (data.latest_run) {
    html += `<h3>Latest Run</h3>
      <div class="card">
        <span class="stat"><span class="stat-label">Job</span><br><span class="stat-val">${data.latest_run.job_name}</span></span>
        <span class="stat"><span class="stat-label">Status</span><br><span class="stat-val ${data.latest_run.status === 'completed' ? '' : 'critical'}">${data.latest_run.status}</span></span>
        <span class="stat"><span class="stat-label">Last Run</span><br><span class="stat-val" style="font-size:0.9rem">${data.latest_run.last_run_at?.substring(0, 19) || 'never'}</span></span>
      </div>`;
  }

  if (data.journal?.length) {
    html += `<h3>Recent Journal (last ${data.journal.length})</h3>
      <table><tr><th>Time</th><th>Action</th><th>Details</th></tr>`;
    for (const j of data.journal.slice(0, 20)) {
      let details = '';
      try { details = JSON.stringify(JSON.parse(j.details || '{}')).substring(0, 120); } catch(e) {}
      html += `<tr>
        <td style="font-size:0.75rem">${j.timestamp?.substring(0, 19) || ''}</td>
        <td>${j.action}</td>
        <td style="font-size:0.7rem;color:#888">${esc(details)}</td>
      </tr>`;
    }
    html += '</table>';
  }

  document.getElementById('content').innerHTML = html;
}

async function loadCoverage() {
  setActive('nav-cov');
  const resp = await fetch('/api/coverage');
  const data = await resp.json();
  let html = `<h2>Source Coverage Matrix</h2>
    <div class="card">
      <span class="stat"><span class="stat-label">Portfolio</span><br><span class="stat-val">${data.total}</span></span>
      <span class="stat"><span class="stat-label">GLEIF (legal)</span><br><span class="stat-val">${data.with_lei}</span></span>
      <span class="stat"><span class="stat-label">RSS Feeds</span><br><span class="stat-val">${data.with_feed}</span></span>
      <span class="stat"><span class="stat-label">Annual Reports</span><br><span class="stat-val">${data.with_annual_report}</span></span>
      <span class="stat"><span class="stat-label">Web Monitor</span><br><span class="stat-val">${data.web_only}</span></span>
      <span class="stat"><span class="stat-label">Unreachable</span><br><span class="stat-val">${data.gap}</span></span>
    </div>`;

  // Per-source breakdown
  html += '<table><tr><th>Source</th><th>Companies</th><th>Coverage</th><th>Bar</th></tr>';
  for (const s of data.sources) {
    const pct = (s.count / data.total * 100).toFixed(0);
    html += `<tr>
      <td>${s.name}</td>
      <td>${s.count}</td>
      <td>${pct}%</td>
      <td><div class=\"bar-bg\"><div class=\"bar\" style=\"width:${pct}%\"></div></div></td>
    </tr>`;
  }

  // Coverage gaps detail
  if (data.coverage_gaps?.length) {
    html += `<h3>Coverage Gaps</h3><p class=\"explanation\">Companies with minimal monitoring coverage.</p>`;
    html += '<table><tr><th>Company</th><th>GLEIF</th><th>Feed</th><th>Annual Report</th></tr>';
    for (const g of data.coverage_gaps.slice(0, 15)) {
      html += `<tr>
        <td>${esc(g.name)}</td>
        <td>${g.has_lei ? '✓' : '✗'}</td>
        <td>${g.has_feed ? '✓' : '✗'}</td>
        <td>${g.has_annual_report ? '✓' : '✗'}</td>
      </tr>`;
    }
    html += '</table>';
  }

  html += '</table>';
  document.getElementById('content').innerHTML = html;
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

loadCompanies();
</script>
</body>
</html>"""

    def _get_companies(self) -> list[dict]:
        rows = self.db.execute("""
            SELECT r.*,
                   COUNT(e.id) as event_count,
                   MAX(s.analyst_priority) as analyst_priority,
                   MAX(e.severity) as max_severity,
                   (SELECT COUNT(*) FROM edges WHERE source_registry_id = r.id) as edge_count,
                   (SELECT MAX(economic_materiality) FROM edges WHERE source_registry_id = r.id) as has_materiality
            FROM registry r
            LEFT JOIN events e ON r.id = e.registry_id AND e.active = 1 AND e.status = 'active'
            LEFT JOIN scores s ON r.id = s.registry_id
            WHERE r.domain IS NOT NULL
            GROUP BY r.id
            ORDER BY analyst_priority DESC
        """).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["materiality_known"] = (
                d.get("has_materiality") is not None
                and float(d["has_materiality"] or 0) not in (0.8, 0.6, 0.5, 0.3, 0.2, 0)
            )
            result.append(d)
        return result

    def _get_company_detail(self, reg_id: int) -> dict:
        row = self.db.execute(
            "SELECT * FROM registry WHERE id = ?", (reg_id,)
        ).fetchone()
        if not row:
            return {"error": "not found"}
        events = self.db.execute(
            """SELECT * FROM events WHERE registry_id = ?
               AND active = 1 AND status = 'active'
               ORDER BY extracted_at DESC""", (reg_id,)
        ).fetchall()
        result = dict(row)
        result["events"] = [dict(e) for e in events]
        return result

    def _get_scores(self) -> list[dict]:
        rows = self.db.execute("""
            SELECT s.*, r.canonical_name
            FROM scores s
            JOIN registry r ON s.registry_id = r.id
            WHERE s.computed_at = (
                SELECT MAX(computed_at) FROM scores s2 WHERE s2.registry_id = s.registry_id
            )
            AND r.domain IS NOT NULL
            ORDER BY s.analyst_priority DESC
        """).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # Parse factors_json for decomposed scores
            if d.get("factors_json"):
                try:
                    factors = json.loads(d["factors_json"])
                    d["signal_score"] = factors.get("signal_score")
                    d["recency_score"] = factors.get("recency_score")
                    d["credibility_score"] = factors.get("credibility_score")
                    d["materiality_score"] = factors.get("materiality_score")
                    d["materiality_known"] = factors.get("materiality_known", False)
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(d)
        return result

    def _get_events(self, registry_id: str | None = None) -> list[dict]:
        if registry_id:
            rows = self.db.execute(
                """SELECT e.*, r.canonical_name
                   FROM events e JOIN registry r ON e.registry_id = r.id
                   WHERE e.registry_id = ? AND e.active = 1 AND e.status = 'active'
                   ORDER BY e.extracted_at DESC""",
                (int(registry_id),),
            ).fetchall()
        else:
            rows = self.db.execute(
                """SELECT e.*, r.canonical_name
                   FROM events e JOIN registry r ON e.registry_id = r.id
                   WHERE e.active = 1 AND e.status = 'active'
                   ORDER BY e.extracted_at DESC LIMIT 100"""
            ).fetchall()
        return [dict(r) for r in rows]

    def _get_sources(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM sources ORDER BY source_name").fetchall()
        return [dict(r) for r in rows]

    def _get_graph(self, registry_id: str | None = None) -> dict:
        if registry_id:
            from kassandra.scoring import compute_legal_ownership_exposure_for_company, _is_graph_capped
            result = compute_legal_ownership_exposure_for_company(self.db, int(registry_id))
            result["graph_capped"] = _is_graph_capped(self.db, int(registry_id))

            # Rename edges → legal_edges for JS clarity
            result["legal_edges"] = result.pop("edges", [])

            # Add economic dependency edges
            eco_rows = self.db.execute(
                """SELECT canonical_name, entity_type, description
                   FROM economic_entities
                   WHERE registry_id = ?
                   ORDER BY entity_type, id DESC""",
                (int(registry_id),),
            ).fetchall()
            result["economic_edges"] = [
                {"name": r["canonical_name"], "type": r["entity_type"],
                 "description": r["description"]}
                for r in eco_rows
            ]
            return result

        total_edges = self.db.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
        edge_types = self.db.execute(
            "SELECT relationship_type, COUNT(*) as c FROM edges GROUP BY relationship_type"
        ).fetchall()
        null_mat = self.db.execute(
            "SELECT COUNT(*) as c FROM edges WHERE economic_materiality IS NULL"
        ).fetchone()["c"]

        return {
            "total_edges": total_edges,
            "edge_types": [{"type": e["relationship_type"], "count": e["c"]} for e in edge_types],
            "null_materiality": null_mat,
            "materiality_unknown": null_mat == total_edges,
        }

    def _get_observability(self) -> dict:
        from kassandra.observability import get_source_health_report, get_latest_run
        return {
            "source_health": get_source_health_report(self.db),
            "latest_run": get_latest_run(self.db),
            "journal": self.db.execute(
                "SELECT * FROM journal ORDER BY id DESC LIMIT 30"
            ).fetchall(),
        }

    def _get_coverage(self) -> dict:
        """Source coverage matrix — which companies are covered by which sources."""
        total = self.db.execute(
            "SELECT COUNT(*) FROM registry WHERE domain IS NOT NULL"
        ).fetchone()[0]
        with_lei = self.db.execute(
            "SELECT COUNT(*) FROM registry WHERE lei IS NOT NULL AND domain IS NOT NULL"
        ).fetchone()[0]
        with_feed = self.db.execute(
            "SELECT COUNT(*) FROM registry WHERE feed_url IS NOT NULL AND domain IS NOT NULL"
        ).fetchone()[0]
        with_annual = self.db.execute(
            "SELECT COUNT(DISTINCT registry_id) FROM economic_entities WHERE registry_id IS NOT NULL AND entity_type != 'jurisdiction'"
        ).fetchone()[0]
        from kassandra.sources.known_feeds import KNOWN_FEEDS, NO_FEED_ISINS
        web_only = total - len(KNOWN_FEEDS) - len(NO_FEED_ISINS)

        sources = [
            {"name": "GLEIF (legal ownership)", "count": with_lei},
            {"name": "Known RSS feeds", "count": len(KNOWN_FEEDS)},
            {"name": "Web monitor (homepage+IR)", "count": web_only},
            {"name": "Annual report extraction", "count": with_annual},
            {"name": "Companies House (UK)", "count": self.db.execute(
                "SELECT COUNT(*) FROM registry WHERE companies_house_number IS NOT NULL AND domain IS NOT NULL"
            ).fetchone()[0]},
        ]

        # Companies with minimal coverage
        gaps = self.db.execute(
            """SELECT r.canonical_name as name,
                      r.lei IS NOT NULL as has_lei,
                      r.feed_url IS NOT NULL as has_feed,
                      (SELECT COUNT(*) FROM economic_entities ee WHERE ee.registry_id = r.id AND ee.entity_type != 'jurisdiction') > 0 as has_annual_report
               FROM registry r WHERE r.domain IS NOT NULL
               ORDER BY has_lei + has_feed + has_annual_report
               LIMIT 15"""
        ).fetchall()

        return {
            "total": total,
            "with_lei": with_lei,
            "with_feed": len(KNOWN_FEEDS),
            "with_annual_report": with_annual,
            "web_only": web_only,
            "gap": 0,  # All 50 have at least GLEIF
            "sources": sources,
            "coverage_gaps": [dict(r) for r in gaps],
        }
