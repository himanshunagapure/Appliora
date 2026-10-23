"""
Phase 11 — local web dashboard for pipeline status (CLI-only was Phase 9).

  python cli.py dashboard

Binds to 127.0.0.1 by default. Read-only JSON + HTML; no submit/approve actions.
"""

from __future__ import annotations

import json
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from core.status import STATUS_ORDER, get_status_counts, list_jobs
from utils.logger import get_logger

logger = get_logger("jobapplypro.dashboard")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Stored off the handler class — a function on BaseHTTPRequestHandler
# would be bound as a method and called with `self`.
_SESSION_SCOPE: Callable | None = None


def build_dashboard_html(counts: dict[str, Any], jobs: list[dict[str, Any]]) -> str:
    cards = []
    by_status = counts.get("by_status") or {}
    for status in STATUS_ORDER:
        n = int(by_status.get(status, 0) or 0)
        cards.append(
            f'<div class="card"><div class="n">{n}</div>'
            f'<div class="k">{escape(status)}</div></div>'
        )
    rows = []
    for job in jobs:
        score = job.get("score")
        score_s = f"{score:.1f}" if isinstance(score, (int, float)) else "—"
        url = job.get("url") or ""
        title = escape(str(job.get("title") or ""))
        company = escape(str(job.get("company") or ""))
        link = f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener">{title}</a>' if url else title
        rows.append(
            "<tr>"
            f"<td>{job.get('id')}</td>"
            f"<td>{escape(str(job.get('status') or ''))}</td>"
            f"<td>{score_s}</td>"
            f"<td>{link}</td>"
            f"<td>{company}</td>"
            f"<td>{escape(str(job.get('source') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="6" class="empty">No jobs yet. Run python cli.py pipeline.</td></tr>')

    runs = []
    for r in counts.get("recent_runs") or []:
        err = " · errors" if r.get("has_errors") else ""
        runs.append(
            f"<li>#{r.get('id')} <strong>{escape(str(r.get('stage') or ''))}</strong> "
            f"jobs={r.get('jobs_processed')} finished={escape(str(r.get('finished_at') or '—'))}{err}</li>"
        )
    runs_html = "".join(runs) or "<li>No run logs yet.</li>"

    total = counts.get("total", 0)
    threshold = counts.get("score_threshold", 6.0)
    approved = counts.get("apply_pending_approved", 0)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>JobApplyPro</title>
  <style>
    :root {{ --bg:#0f1419; --panel:#1a222c; --ink:#e8eef4; --muted:#8b9aab;
            --accent:#c4a35a; --line:#2a3542; --ok:#3d9a6a; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family: "Segoe UI", system-ui, sans-serif;
           background: var(--bg); color: var(--ink); }}
    header {{ padding: 1.4rem 1.6rem 0.6rem; border-bottom: 1px solid var(--line); }}
    header h1 {{ margin:0; font-size: 1.35rem; letter-spacing: .02em; }}
    header p {{ margin: .35rem 0 0; color: var(--muted); font-size: .9rem; }}
    .wrap {{ padding: 1.2rem 1.6rem 3rem; max-width: 1200px; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(120px,1fr));
             gap: .75rem; margin: 1rem 0 1.4rem; }}
    .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
             padding: .85rem .9rem; }}
    .card .n {{ font-size: 1.55rem; font-weight: 700; color: var(--accent); }}
    .card .k {{ font-size: .72rem; color: var(--muted); text-transform: uppercase;
                letter-spacing: .04em; margin-top: .15rem; }}
    .meta {{ color: var(--muted); font-size: .88rem; margin-bottom: 1rem; }}
    table {{ width:100%; border-collapse: collapse; background: var(--panel);
             border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }}
    th, td {{ text-align:left; padding: .55rem .7rem; border-bottom: 1px solid var(--line);
              font-size: .88rem; }}
    th {{ color: var(--muted); font-weight: 600; font-size: .75rem;
          text-transform: uppercase; letter-spacing: .04em; }}
    a {{ color: #8cb4e8; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .empty {{ color: var(--muted); text-align:center; padding: 1.4rem; }}
    h2 {{ font-size: 1rem; margin: 1.4rem 0 .5rem; }}
    ul.runs {{ color: var(--muted); font-size: .85rem; line-height: 1.55; padding-left: 1.1rem; }}
  </style>
</head>
<body>
  <header>
    <h1>JobApplyPro</h1>
    <p>Local dashboard · {total} jobs · threshold {threshold} · approved &amp; ready to submit: {approved}</p>
  </header>
  <div class="wrap">
    <div class="grid">{"".join(cards)}</div>
    <p class="meta">Read-only. Approve/submit still happen in the CLI.
      Refresh this page or wait — data reloads every 30s.</p>
    <h2>Recent jobs</h2>
    <table>
      <thead><tr><th>ID</th><th>Status</th><th>Score</th><th>Title</th><th>Company</th><th>Source</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    <h2>Recent runs</h2>
    <ul class="runs">{runs_html}</ul>
  </div>
  <script>
    setTimeout(function() {{ location.reload(); }}, 30000);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        logger.debug("%s - " + fmt, self.address_string(), *args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        try:
            if path == "/api/status":
                data = get_status_counts(session_scope=_SESSION_SCOPE)
                self._json(200, data)
                return
            if path == "/api/jobs":
                status = (qs.get("status") or [None])[0] or None
                try:
                    limit = int((qs.get("limit") or ["80"])[0])
                except ValueError:
                    limit = 80
                jobs = list_jobs(
                    session_scope=_SESSION_SCOPE,
                    status=status,
                    limit=max(1, min(limit, 200)),
                )
                self._json(200, {"jobs": jobs})
                return
            if path in {"/", "/index.html"}:
                counts = get_status_counts(session_scope=_SESSION_SCOPE)
                jobs = list_jobs(session_scope=_SESSION_SCOPE, limit=80)
                html = build_dashboard_html(counts, jobs)
                payload = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            self._json(404, {"error": "not found"})
        except Exception as exc:
            logger.exception("Dashboard request failed: %s", exc)
            self._json(500, {"error": str(exc)})

    def _json(self, code: int, data: Any) -> None:
        payload = json.dumps(data, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def serve_dashboard(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    session_scope: Callable | None = None,
) -> None:
    global _SESSION_SCOPE
    _SESSION_SCOPE = session_scope
    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    url = f"http://{host}:{port}/"
    logger.info("Dashboard listening on %s", url)
    print(f"\nJobApplyPro dashboard: {url}\nCtrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        httpd.server_close()
        _SESSION_SCOPE = None
