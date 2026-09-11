"""Phase 8 review gate — list / approve / reject / submit (no live employers)."""

from __future__ import annotations

import json
from argparse import Namespace
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.apply import ApplyError, run_apply_stage
from core.review import (
    ReviewError,
    approve_job,
    format_review_card,
    list_pending_reviews,
    reject_job,
    run_interactive_review,
)
from db.models import Application, Base, Job
from automation.types import FillResult

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


def _seed_pending(session_scope, tmp_path, *, n=1):
    resume = tmp_path / "resume.md"
    resume.write_text("# Jane Doe\nLangGraph\n")
    cover = tmp_path / "cover.md"
    cover.write_text("Dear hiring manager,\n")
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n")
    ids = []
    with session_scope() as session:
        for i in range(n):
            job = Job(
                url=f"https://careers.example.com/apply/{i}",
                title=f"Engineer {i}",
                company="Acme",
                source="indeed",
                description="Build stuff",
                status="apply_pending",
                score=8.0 - i,
                reasoning="Apply(dry-run) adapter=generic status=filled",
            )
            session.add(job)
            session.flush()
            session.add(
                Application(
                    job_id=job.id,
                    tailored_resume_path=str(resume),
                    cover_letter_path=str(cover),
                    screenshot_path=str(shot),
                )
            )
            ids.append(job.id)
    return ids, resume, cover, shot


def test_list_pending_review_includes_materials(tmp_path):
    session_scope = make_temp_sqlite_session_scope()
    ids, resume, cover, shot = _seed_pending(session_scope, tmp_path)
    items = list_pending_reviews(session_scope=session_scope)
    assert len(items) == 1
    item = items[0]
    assert item.job_id == ids[0]
    assert item.screenshot_path == str(shot)
    assert item.tailored_resume_path == str(resume)
    card = format_review_card(item)
    assert "Jane Doe" in card
    assert "Dear hiring manager" in card
    assert str(shot) in card
    assert "review=pending" in card


def test_approve_then_submit_sets_applied(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(FIXTURE_PROFILE))

    session_scope = make_temp_sqlite_session_scope()
    ids, *_ = _seed_pending(session_scope, tmp_path)
    jid = ids[0]

    item = approve_job(jid, session_scope=session_scope)
    assert item.is_approved

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
        app = session.query(Application).one()
        assert job.status == "applied"
        assert app.submitted_at is not None
        assert app.review_decision == "approved"


def test_reject_sets_skipped_and_blocks_submit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(FIXTURE_PROFILE))

    session_scope = make_temp_sqlite_session_scope()
    ids, *_ = _seed_pending(session_scope, tmp_path)
    jid = ids[0]
    item = reject_job(jid, reason="wrong location", session_scope=session_scope)
    assert item.status == "skipped"
    assert item.review_decision == "rejected"

    with pytest.raises(ApplyError, match="apply_pending"):
        run_apply_stage(
            submit=True,
            dry_run=False,
            job_id=jid,
            session_scope=session_scope,
            profile_path=profile_path,
            apply_fn=lambda *a, **k: FillResult(status="submitted"),
        )


def test_approve_wrong_status_raises(tmp_path):
    session_scope = make_temp_sqlite_session_scope()
    with session_scope() as session:
        job = Job(
            url="https://careers.example.com/x",
            title="Eng",
            company="A",
            source="indeed",
            description="x",
            status="cover_letter_ready",
            score=7.0,
        )
        session.add(job)
        session.flush()
        jid = job.id
    with pytest.raises(ReviewError, match="cover_letter_ready"):
        approve_job(jid, session_scope=session_scope)


def test_interactive_review_approve_then_skip(tmp_path):
    session_scope = make_temp_sqlite_session_scope()
    _seed_pending(session_scope, tmp_path, n=2)
    answers = iter(["a", "s"])
    out = StringIO()
    summary = run_interactive_review(
        session_scope=session_scope,
        input_fn=lambda _prompt: next(answers),
        output=out,
    )
    assert summary["shown"] == 2
    assert summary["approved"] == 1
    assert summary["skipped"] == 1
    items = list_pending_reviews(session_scope=session_scope)
    assert len(items) == 2  # skip leaves apply_pending; approve also stays pending
    assert sum(1 for i in items if i.is_approved) == 1


def test_cli_review_approve_and_list(tmp_path, monkeypatch, capsys):
    from cli import cmd_review

    session_scope = make_temp_sqlite_session_scope()
    ids, *_ = _seed_pending(session_scope, tmp_path)
    jid = ids[0]
    monkeypatch.setattr("core.review.get_db_session", session_scope)

    rc = cmd_review(
        Namespace(
            approve=False,
            reject=False,
            job_id=None,
            interactive=False,
            open=False,
            reason=None,
        )
    )
    assert rc == 0
    listed = capsys.readouterr().out
    assert f"Job {jid}" in listed
    assert "screenshot:" in listed

    rc = cmd_review(
        Namespace(
            approve=True,
            reject=False,
            job_id=jid,
            interactive=False,
            open=False,
            reason=None,
        )
    )
    assert rc == 0
    assert "Approved" in capsys.readouterr().out

    items = list_pending_reviews(job_id=jid, session_scope=session_scope)
    assert items[0].is_approved


def test_dry_run_refill_clears_stale_approval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(FIXTURE_PROFILE))

    session_scope = make_temp_sqlite_session_scope()
    ids, *_ = _seed_pending(session_scope, tmp_path)
    jid = ids[0]
    approve_job(jid, session_scope=session_scope)

    def fake_fill(*args, **kwargs):
        assert kwargs.get("dry_run") is True
        return FillResult(status="filled", adapter="generic")

    summary = run_apply_stage(
        dry_run=True,
        job_id=jid,
        session_scope=session_scope,
        profile_path=profile_path,
        apply_fn=fake_fill,
    )
    assert summary["apply_pending"] == 1
    with session_scope() as session:
        app = session.query(Application).one()
        assert app.review_decision is None
        assert session.query(Job).one().status == "apply_pending"

    with pytest.raises(ApplyError, match="not approved"):
        run_apply_stage(
            submit=True,
            dry_run=False,
            job_id=jid,
            session_scope=session_scope,
            profile_path=profile_path,
            apply_fn=lambda *a, **k: FillResult(status="submitted"),
        )


def test_cli_rejects_dry_run_and_submit_together():
    from cli import cmd_run

    rc = cmd_run(
        Namespace(
            stage="apply",
            dry_run=True,
            submit=True,
            job_id=1,
        )
    )
    assert rc == 1
