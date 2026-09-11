"""
Low-stakes Phase 8 e2e: local HTML job posting (no real employer).

Dry-run fill → review approve → submit. Asserts the form POST happened
and job status becomes applied.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Application, Base, Job

FIXTURE_HTML = (
    Path(__file__).parent / "fixtures" / "forms" / "local_apply.html"
).read_text(encoding="utf-8")
FIXTURE_PROFILE = json.loads(
    (Path(__file__).parent / "fixtures" / "sample_profile.json").read_text()
)


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


def make_temp_sqlite_session_scope():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    @contextmanager
    def session_scope():
        session = SessionLocal()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    return session_scope


class _ApplyHandler(BaseHTTPRequestHandler):
    posts = 0

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        body = FIXTURE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        type(self).posts += 1
        body = b"<html><body><h1>Thanks</h1><p>Application received.</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_approve_then_submit_local_posting(tmp_path, monkeypatch):
    if not _chromium_available():
        pytest.skip("Playwright Chromium is not installed")

    from core.apply import run_apply_stage
    from core.review import approve_job
    from config import settings as settings_mod

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile = dict(FIXTURE_PROFILE)
    profile["contact"] = dict(profile["contact"])
    profile["contact"]["phone"] = "555-0100"
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(profile))

    resume = tmp_path / "resume.md"
    resume.write_text("# Jane Doe\nSkill: LangGraph\n")
    cover = tmp_path / "cover.md"
    cover.write_text("Dear hiring manager,\nI would like to apply.\n")
    ats_path = tmp_path / "ats.yaml"
    ats_path.write_text(
        "tier2_ats: []\nmanual_ats: []\nblocked:\n  sites: []\n  url_patterns: []\n"
        "blocked_sso: []\ncaptcha_signals: []\n"
    )

    fake_settings = MagicMock()
    fake_settings.tier2_enabled = False
    fake_settings.tier2_max_jobs_per_run = 0
    fake_settings.tier2_max_agent_steps = 5
    fake_settings.tier2_llm_model = ""
    fake_settings.gemini_model = "gemini-2.5-flash"
    fake_settings.gemini_api_key = "test-key"
    fake_settings.browser_headless = True
    fake_settings.browser_user_data_dir = None
    fake_settings.browser_channel = None
    fake_settings.browser_login_wait_seconds = 1
    fake_settings.form_min_confidence = 0.55
    fake_settings.screenshot_output_dir = tmp_path / "shots"
    fake_settings.ats_config_path = ats_path
    fake_settings.profile_output_path = profile_path
    monkeypatch.setattr(settings_mod, "_settings", fake_settings)
    monkeypatch.setattr("core.apply.get_settings", lambda: fake_settings)
    monkeypatch.setattr("automation.browser.get_settings", lambda: fake_settings)
    monkeypatch.setattr("automation.form_detector.get_settings", lambda: fake_settings)
    monkeypatch.setattr("automation.agent_apply.get_settings", lambda: fake_settings)

    _ApplyHandler.posts = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApplyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    job_url = f"http://{host}:{port}/job"

    session_scope = make_temp_sqlite_session_scope()
    try:
        with session_scope() as session:
            job = Job(
                url=job_url,
                title="Software Engineer",
                company="Local Test Co",
                source="fixture",
                description="Low-stakes local apply form for Phase 8.",
                status="cover_letter_ready",
                score=9.0,
            )
            session.add(job)
            session.flush()
            jid = job.id
            session.add(
                Application(
                    job_id=jid,
                    tailored_resume_path=str(resume),
                    cover_letter_path=str(cover),
                )
            )

        dry = run_apply_stage(
            dry_run=True,
            session_scope=session_scope,
            profile_path=profile_path,
            screenshot_dir=tmp_path / "shots",
        )
        assert dry["apply_pending"] == 1
        assert _ApplyHandler.posts == 0

        item = approve_job(jid, session_scope=session_scope)
        assert item.is_approved

        submitted = run_apply_stage(
            submit=True,
            dry_run=False,
            job_id=jid,
            session_scope=session_scope,
            profile_path=profile_path,
            screenshot_dir=tmp_path / "shots",
        )
        assert submitted["applied"] == 1
        assert _ApplyHandler.posts >= 1

        with session_scope() as session:
            job = session.query(Job).one()
            app = session.query(Application).one()
            assert job.status == "applied"
            assert app.submitted_at is not None
            assert app.review_decision == "approved"
            assert app.screenshot_path
            assert Path(app.screenshot_path).exists()
    finally:
        server.shutdown()
        server.server_close()
