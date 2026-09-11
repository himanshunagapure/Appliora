"""Stage tests for core.apply — mocked browser / apply_fn, no Chromium."""

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.types import FillResult
from core.apply import ApplyError, run_apply_stage, select_adapter
from db.models import Application, Base, Job

FIXTURE_PROFILE = json.loads(
    (Path(__file__).parent / "fixtures" / "sample_profile.json").read_text()
)


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


def test_select_adapter_prefers_linkedin_then_workday_greenhouse_then_generic():
    assert select_adapter("https://www.linkedin.com/jobs/view/1").name == "linkedin_easy_apply"
    assert (
        select_adapter("https://x.myworkdayjobs.com/en-US/job/1").name == "workday"
    )
    assert (
        select_adapter(
            "https://www.precisely.com/careers/job/?gh_jid=123"
        ).name
        == "greenhouse"
    )
    assert select_adapter("https://careers.example.com/job/1").name == "generic"


def test_run_apply_stage_dry_run_sets_apply_pending(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(FIXTURE_PROFILE))
    shot_dir = tmp_path / "output" / "screenshots"

    session_scope = make_temp_sqlite_session_scope()
    with session_scope() as session:
        job = Job(
            url="https://careers.example.com/apply/1",
            title="Engineer",
            company="Acme",
            source="indeed",
            description="Build stuff",
            status="cover_letter_ready",
            score=8.0,
        )
        other = Job(
            url="https://careers.example.com/apply/2",
            title="Still tailored",
            company="Beta",
            source="indeed",
            description="x",
            status="tailored",
            score=9.0,
        )
        session.add_all([job, other])
        session.flush()
        session.add(
            Application(
                job_id=job.id,
                tailored_resume_path=str(tmp_path / "r.md"),
                cover_letter_path=str(tmp_path / "c.txt"),
            )
        )

    def fake_apply(job, application, profile, **kwargs):
        shot = shot_dir / f"job_{job.id}.png"
        shot_dir.mkdir(parents=True, exist_ok=True)
        shot.write_bytes(b"fake")
        return FillResult(
            status="filled",
            screenshot_path=str(shot),
            fields_filled=["email"],
            adapter="generic",
        )

    summary = run_apply_stage(
        dry_run=True,
        session_scope=session_scope,
        profile_path=profile_path,
        screenshot_dir=shot_dir,
        apply_fn=fake_apply,
    )

    assert summary["processed"] == 1
    assert summary["apply_pending"] == 1
    assert summary["failed"] == 0

    with session_scope() as session:
        jobs = {j.url: j for j in session.query(Job).all()}
        assert jobs["https://careers.example.com/apply/1"].status == "apply_pending"
        assert jobs["https://careers.example.com/apply/2"].status == "tailored"
        app = (
            session.query(Application)
            .filter_by(job_id=jobs["https://careers.example.com/apply/1"].id)
            .one()
        )
        assert app.screenshot_path
        assert Path(app.screenshot_path).exists()


def test_run_apply_stage_captcha_routes_manual_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(FIXTURE_PROFILE))

    session_scope = make_temp_sqlite_session_scope()
    with session_scope() as session:
        session.add(
            Job(
                url="https://careers.example.com/apply/c",
                title="Eng",
                company="A",
                source="indeed",
                description="x",
                status="cover_letter_ready",
                score=7.0,
            )
        )

    def fake_apply(*args, **kwargs):
        return FillResult(status="manual_review", error="captcha")

    summary = run_apply_stage(
        dry_run=True,
        session_scope=session_scope,
        profile_path=profile_path,
        screenshot_dir=tmp_path / "shots",
        apply_fn=fake_apply,
    )
    assert summary["manual_review"] == 1
    with session_scope() as session:
        assert session.query(Job).one().status == "manual_review"


def test_run_apply_stage_submit_requires_job_id():
    with pytest.raises(ApplyError, match="job-id"):
        run_apply_stage(submit=True, dry_run=False)


def test_run_apply_stage_submit_updates_applied(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(FIXTURE_PROFILE))

    session_scope = make_temp_sqlite_session_scope()
    with session_scope() as session:
        job = Job(
            url="https://careers.example.com/apply/s",
            title="Eng",
            company="A",
            source="indeed",
            description="x",
            status="apply_pending",
            score=8.0,
        )
        session.add(job)
        session.flush()
        jid = job.id
        session.add(
            Application(job_id=jid, review_decision="approved")
        )

    def fake_apply(*args, **kwargs):
        assert kwargs.get("dry_run") is False
        return FillResult(status="submitted", adapter="generic")

    summary = run_apply_stage(
        submit=True,
        dry_run=False,
        job_id=jid,
        session_scope=session_scope,
        profile_path=profile_path,
        apply_fn=fake_apply,
    )
    assert summary["applied"] == 1
    with session_scope() as session:
        job = session.query(Job).one()
        assert job.status == "applied"
        app = session.query(Application).one()
        assert app.submitted_at is not None


def test_run_apply_stage_submit_refuses_without_approval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(FIXTURE_PROFILE))

    session_scope = make_temp_sqlite_session_scope()
    with session_scope() as session:
        job = Job(
            url="https://careers.example.com/apply/unapproved",
            title="Eng",
            company="A",
            source="indeed",
            description="x",
            status="apply_pending",
            score=8.0,
        )
        session.add(job)
        session.flush()
        jid = job.id
        session.add(Application(job_id=jid))

    called = {"n": 0}

    def fake_apply(*args, **kwargs):
        called["n"] += 1
        return FillResult(status="submitted", adapter="generic")

    with pytest.raises(ApplyError, match="not approved"):
        run_apply_stage(
            submit=True,
            dry_run=False,
            job_id=jid,
            session_scope=session_scope,
            profile_path=profile_path,
            apply_fn=fake_apply,
        )
    assert called["n"] == 0
    with session_scope() as session:
        assert session.query(Job).one().status == "apply_pending"


def test_run_apply_stage_skips_blocked_and_expired(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(FIXTURE_PROFILE))

    session_scope = make_temp_sqlite_session_scope()
    with session_scope() as session:
        session.add_all(
            [
                Job(
                    url="https://www.glassdoor.com/job/1",
                    title="Blocked",
                    company="A",
                    source="glassdoor",
                    description="x",
                    status="cover_letter_ready",
                    score=9.0,
                ),
                Job(
                    url="https://careers.example.com/expired",
                    title="Expired",
                    company="B",
                    source="indeed",
                    description="x",
                    status="cover_letter_ready",
                    score=8.0,
                ),
            ]
        )

    def fake_apply(job, *args, **kwargs):
        if "glassdoor" in job.url:
            return FillResult(status="skipped", error="site_blocked")
        return FillResult(status="expired", error="expired: closed")

    summary = run_apply_stage(
        dry_run=True,
        session_scope=session_scope,
        profile_path=profile_path,
        screenshot_dir=tmp_path,
        apply_fn=fake_apply,
    )
    assert summary["skipped"] == 1
    assert summary["failed"] == 1  # expired maps to failed job status
    with session_scope() as session:
        jobs = {j.title: j for j in session.query(Job).all()}
        assert jobs["Blocked"].status == "skipped"
        assert jobs["Expired"].status == "failed"


def test_run_apply_stage_isolates_per_job_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(FIXTURE_PROFILE))

    session_scope = make_temp_sqlite_session_scope()
    with session_scope() as session:
        session.add_all(
            [
                Job(
                    url="https://a.com/1",
                    title="Good",
                    company="A",
                    source="indeed",
                    description="x",
                    status="cover_letter_ready",
                    score=9.0,
                ),
                Job(
                    url="https://a.com/2",
                    title="Bad",
                    company="B",
                    source="indeed",
                    description="x",
                    status="cover_letter_ready",
                    score=8.0,
                ),
            ]
        )

    def fake_apply(job, *args, **kwargs):
        if job.title == "Bad":
            raise RuntimeError("boom")
        return FillResult(
            status="filled",
            screenshot_path=str(tmp_path / "s.png"),
            adapter="generic",
        )

    (tmp_path / "s.png").write_bytes(b"x")
    summary = run_apply_stage(
        dry_run=True,
        session_scope=session_scope,
        profile_path=profile_path,
        screenshot_dir=tmp_path,
        apply_fn=fake_apply,
    )
    assert summary["processed"] == 2
    assert summary["apply_pending"] == 1
    assert summary["failed"] == 1


def test_apply_to_job_routes_lever_to_tier2(tmp_path, monkeypatch):
    """Lever domains skip Tier 1 and use the Tier 2 agent (ADR-002)."""
    from core.apply import apply_to_job

    monkeypatch.chdir(tmp_path)
    resume = tmp_path / "r.md"
    resume.write_text("# resume")
    job = SimpleNamespace(
        id=1,
        url="https://jobs.lever.co/acme/abc",
        title="Eng",
        company="Acme",
    )
    application = SimpleNamespace(
        tailored_resume_path=str(resume),
        cover_letter_path=None,
    )
    profile = FIXTURE_PROFILE

    async def fake_runner(**kwargs):
        class H:
            def final_result(self):
                return "Done. outcome=filled"

            def is_successful(self):
                return True

            def is_done(self):
                return True

            def errors(self):
                return []

            def urls(self):
                return [job.url]

            def screenshot_paths(self):
                return []

        return H()

    # Settings / ats config for the call
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from config import settings as settings_mod

    fake_settings = MagicMock()
    fake_settings.tier2_enabled = True
    fake_settings.tier2_max_agent_steps = 10
    fake_settings.tier2_max_jobs_per_run = 5
    fake_settings.tier2_llm_model = ""
    fake_settings.gemini_model = "gemini-2.5-flash"
    fake_settings.gemini_api_key = "test-key"
    fake_settings.browser_headless = True
    fake_settings.browser_user_data_dir = None
    fake_settings.form_min_confidence = 0.55
    fake_settings.screenshot_output_dir = tmp_path / "shots"
    fake_settings.ats_config_path = tmp_path / "ats.yaml"
    (tmp_path / "ats.yaml").write_text(
        "tier2_ats:\n  - lever.co\nmanual_ats:\n  - lever.co\nblocked:\n  sites: []\n  url_patterns: []\nblocked_sso: []\ncaptcha_signals: []\n"
    )
    monkeypatch.setattr(settings_mod, "_settings", fake_settings)
    monkeypatch.setattr("core.apply.get_settings", lambda: fake_settings)
    monkeypatch.setattr("automation.agent_apply.get_settings", lambda: fake_settings)

    result = apply_to_job(
        job,
        application,
        profile,
        dry_run=True,
        screenshot_dir=tmp_path / "shots",
        tier2_runner=fake_runner,
    )
    assert result.status == "filled"
    assert result.adapter == "browser_use_tier2"


def test_apply_to_job_unrecognized_form_falls_through_to_tier2(tmp_path, monkeypatch):
    """Low-confidence generic forms leave the Tier 1 browser and use the agent."""
    from core.apply import apply_to_job

    resume = tmp_path / "r.md"
    resume.write_text("# resume")
    job = SimpleNamespace(
        id=9,
        url="https://careers.example.com/role/99",
        title="Eng",
        company="Acme",
    )
    application = SimpleNamespace(
        tailored_resume_path=str(resume),
        cover_letter_path=None,
    )

    class FakePage:
        def content(self):
            return "<html><body><p>Custom widget apply</p></body></html>"

    class FakeBrowser:
        page = FakePage()
        current_url = job.url

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def goto(self, url):
            return None

        def content(self):
            return "<html><body><p>Custom widget apply</p></body></html>"

        def detect_captcha(self, *args, **kwargs):
            return False

    called = {"tier2": 0}

    async def fake_runner(**kwargs):
        called["tier2"] += 1

        class H:
            def final_result(self):
                return "Done. outcome=filled"

            def is_successful(self):
                return True

            def is_done(self):
                return True

            def errors(self):
                return []

            def urls(self):
                return [job.url]

            def screenshot_paths(self):
                return []

        return H()

    fake_settings = MagicMock()
    fake_settings.tier2_enabled = True
    fake_settings.tier2_max_agent_steps = 10
    fake_settings.tier2_max_jobs_per_run = 5
    fake_settings.tier2_llm_model = ""
    fake_settings.gemini_model = "gemini-2.5-flash"
    fake_settings.gemini_api_key = "test-key"
    fake_settings.browser_headless = True
    fake_settings.browser_user_data_dir = None
    fake_settings.form_min_confidence = 0.55
    fake_settings.screenshot_output_dir = tmp_path / "shots"
    fake_settings.ats_config_path = tmp_path / "ats.yaml"
    (tmp_path / "ats.yaml").write_text(
        "tier2_ats: []\nmanual_ats: []\nblocked:\n  sites: []\n  url_patterns: []\n"
        "blocked_sso: []\ncaptcha_signals: []\n"
    )
    from config import settings as settings_mod

    monkeypatch.setattr(settings_mod, "_settings", fake_settings)
    monkeypatch.setattr("core.apply.get_settings", lambda: fake_settings)
    monkeypatch.setattr("automation.agent_apply.get_settings", lambda: fake_settings)
    monkeypatch.setattr("automation.form_detector.get_settings", lambda: fake_settings)

    result = apply_to_job(
        job,
        application,
        FIXTURE_PROFILE,
        dry_run=True,
        browser_factory=FakeBrowser,
        screenshot_dir=tmp_path / "shots",
        tier2_runner=fake_runner,
    )
    assert called["tier2"] == 1
    assert result.status == "filled"
    assert result.adapter == "browser_use_tier2"

