import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.tailor import (
    TailoringError,
    fact_payload,
    find_missing_resume_facts,
    run_tailoring_stage,
    save_tailored_resume,
    tailor_job,
)
from db.models import Application, Base, Job
from llm.prompts.tailor_prompt import build_tailor_prompt

FIXTURE_PROFILE = json.loads(
    (Path(__file__).parent / "fixtures" / "sample_profile.json").read_text()
)


class FakeLLMClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.prompts = []

    def generate_json(self, prompt, max_retries=None):
        self.prompts.append(prompt)
        return self._responses.pop(0)


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


def _resume_with_all_facts(profile: dict) -> str:
    """Build a markdown resume that contains every resume_fact payload."""
    lines = ["# Jane Doe", "", "## Experience"]
    for fact in profile["resume_facts"]:
        lines.append(f"- {fact_payload(fact)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fact validation
# ---------------------------------------------------------------------------

def test_fact_payload_strips_label():
    assert fact_payload("Company: Acme Corp") == "Acme Corp"
    assert fact_payload("plain fact") == "plain fact"


def test_find_missing_resume_facts_detects_alterations():
    facts = ["Company: AIQoD", "Title: AI/ML Developer", "Achievement: Built RAG"]
    text = "Jane worked at AIQoD as AI/ML Developer."
    missing = find_missing_resume_facts(text, facts)
    assert missing == ["Achievement: Built RAG"]


def test_find_missing_resume_facts_passes_when_all_present():
    facts = FIXTURE_PROFILE["resume_facts"]
    text = _resume_with_all_facts(FIXTURE_PROFILE)
    assert find_missing_resume_facts(text, facts) == []


def test_tailor_prompt_includes_never_alter_constraint():
    prompt = build_tailor_prompt(
        profile_json="{}",
        resume_facts=["Company: Acme"],
        job_title="Engineer",
        company="Acme",
        job_description="Build stuff",
    )
    assert "NEVER alter protected resume_facts" in prompt
    assert "Company: Acme" in prompt
    assert "content AFTER" in prompt


# ---------------------------------------------------------------------------
# Per-job tailor
# ---------------------------------------------------------------------------

def test_tailor_job_succeeds_when_facts_preserved():
    good = {
        "tailored_resume": _resume_with_all_facts(FIXTURE_PROFILE),
        "emphasis_notes": "Emphasized LangGraph.",
    }
    client = FakeLLMClient(responses=[good])
    markdown, notes = tailor_job(
        job_title="GenAI Developer",
        company="Startup",
        job_description="LangGraph and RAG experience required.",
        profile=FIXTURE_PROFILE,
        llm_client=client,
    )
    assert "AIQoD" in markdown
    assert "LangGraph" in notes
    assert len(client.prompts) == 1


def test_tailor_job_retries_then_succeeds():
    bad = {"tailored_resume": "# Incomplete\n\nNo facts here.", "emphasis_notes": ""}
    good = {
        "tailored_resume": _resume_with_all_facts(FIXTURE_PROFILE),
        "emphasis_notes": "Fixed facts.",
    }
    client = FakeLLMClient(responses=[bad, good])
    markdown, _ = tailor_job(
        job_title="GenAI Developer",
        company="Startup",
        job_description="Need GenAI.",
        profile=FIXTURE_PROFILE,
        llm_client=client,
        max_attempts=2,
    )
    assert "AIQoD" in markdown
    assert len(client.prompts) == 2
    assert "FAILED fact-preservation" in client.prompts[1]


def test_tailor_job_retries_after_empty_response():
    empty = {"tailored_resume": "", "emphasis_notes": ""}
    good = {
        "tailored_resume": _resume_with_all_facts(FIXTURE_PROFILE),
        "emphasis_notes": "ok",
    }
    client = FakeLLMClient(responses=[empty, good])
    markdown, notes = tailor_job(
        job_title="GenAI Developer",
        company="Startup",
        job_description="Need GenAI.",
        profile=FIXTURE_PROFILE,
        llm_client=client,
        max_attempts=2,
    )
    assert "AIQoD" in markdown
    assert notes == "ok"
    assert len(client.prompts) == 2


def test_tailor_job_raises_after_retry_still_missing_facts():
    bad = {"tailored_resume": "# Still broken", "emphasis_notes": ""}
    client = FakeLLMClient(responses=[bad, bad])
    with pytest.raises(TailoringError, match="resume_facts still missing"):
        tailor_job(
            job_title="X",
            company="Y",
            job_description="Z",
            profile=FIXTURE_PROFILE,
            llm_client=client,
            max_attempts=2,
        )


def test_save_tailored_resume_writes_markdown(tmp_path):
    path = save_tailored_resume(
        job_id=42,
        company="Acme Corp",
        title="GenAI Dev",
        markdown="# Hello",
        output_dir=tmp_path,
    )
    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith("# Hello")
    assert "job_42" in path.name


# ---------------------------------------------------------------------------
# Full stage
# ---------------------------------------------------------------------------

def test_run_tailoring_stage_tailors_jobs_above_threshold(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(FIXTURE_PROFILE))
    out_dir = tmp_path / "output" / "tailored"

    session_scope = make_temp_sqlite_session_scope()
    with session_scope() as session:
        session.add_all(
            [
                Job(
                    url="https://x.com/good",
                    title="GenAI Developer",
                    company="A",
                    source="indeed",
                    description="LangGraph RAG",
                    status="scored",
                    score=8.5,
                ),
                Job(
                    url="https://x.com/low",
                    title="Warehouse",
                    company="B",
                    source="indeed",
                    description="Lift boxes",
                    status="scored",
                    score=2.0,  # below default threshold 6.0
                ),
                Job(
                    url="https://x.com/pending",
                    title="Not scored yet",
                    company="C",
                    source="indeed",
                    description="Whatever",
                    status="discovered",
                    score=None,
                ),
            ]
        )

    good = {
        "tailored_resume": _resume_with_all_facts(FIXTURE_PROFILE),
        "emphasis_notes": "GenAI emphasis.",
    }
    summary = run_tailoring_stage(
        session_scope=session_scope,
        llm_client=FakeLLMClient(responses=[good]),
        profile_path=profile_path,
        output_dir=out_dir,
    )

    assert summary["processed"] == 1
    assert summary["tailored"] == 1
    assert summary["failed"] == 0
    assert summary["manual_review"] == 0

    with session_scope() as session:
        jobs = {j.url: j for j in session.query(Job).all()}
        assert jobs["https://x.com/good"].status == "tailored"
        assert jobs["https://x.com/low"].status == "scored"  # untouched
        assert jobs["https://x.com/pending"].status == "discovered"
        app = session.query(Application).filter_by(job_id=jobs["https://x.com/good"].id).one()
        assert app.tailored_resume_path
        assert Path(app.tailored_resume_path).exists()


def test_run_tailoring_stage_flags_manual_review_on_persistent_fact_failure(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(FIXTURE_PROFILE))

    session_scope = make_temp_sqlite_session_scope()
    with session_scope() as session:
        session.add(
            Job(
                url="https://x.com/bad",
                title="GenAI Developer",
                company="A",
                source="indeed",
                description="LangGraph",
                status="scored",
                score=9.0,
            )
        )

    bad = {"tailored_resume": "# fabricated only", "emphasis_notes": ""}
    summary = run_tailoring_stage(
        session_scope=session_scope,
        llm_client=FakeLLMClient(responses=[bad, bad]),
        profile_path=profile_path,
        output_dir=tmp_path / "output",
    )

    assert summary["processed"] == 1
    assert summary["manual_review"] == 1
    assert summary["tailored"] == 0

    with session_scope() as session:
        job = session.query(Job).one()
        assert job.status == "manual_review"
        assert "fact-check failed" in (job.reasoning or "").lower()
