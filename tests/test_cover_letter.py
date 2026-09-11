import json
import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.cover_letter import (
    CoverLetterError,
    generate_cover_letter,
    run_cover_letter_stage,
    save_cover_letter,
    validate_cover_letter,
)
from db.models import Application, Base, Job
from llm.prompts.cover_letter_prompt import build_cover_letter_prompt

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


def _good_letter(company: str = "Acme", title: str = "GenAI Developer") -> str:
    """A letter that passes validate_cover_letter length + company/title checks."""
    body = (
        f"Dear Hiring Team at {company},\n\n"
        f"I am excited to apply for the {title} role. At AIQoD I built multi-agent "
        f"LangGraph workflows and RAG systems that map closely to what you described. "
        f"My production experience with Pinecone, FastAPI, and autonomous tool generation "
        f"lets me contribute quickly on agent orchestration and retrieval quality. "
        f"I also shipped a lead-generation bot that combined scraping with LLM extraction, "
        f"which taught me how to turn messy web data into actionable pipeline outputs.\n\n"
        f"I would welcome the chance to bring this GenAI background to {company} as a "
        f"{title}, collaborating with your team on reliable LLM features and measurable "
        f"product outcomes. Thank you for considering my application.\n\n"
        f"Sincerely,\nJane Doe\n"
    )
    pad = (
        "I focus on clear APIs, careful evaluation of model outputs, and shipping "
        "incremental improvements that stay grounded in real user needs. "
    )
    while len(re.findall(r"\b\w+\b", body)) < 90:
        body = body.replace("Sincerely,", pad + "Sincerely,", 1)
    return body

# ---------------------------------------------------------------------------
# Validation + prompt
# ---------------------------------------------------------------------------

def test_validate_cover_letter_requires_company_and_title():
    text = _good_letter("Acme", "GenAI Developer")
    assert validate_cover_letter(text, "Acme", "GenAI Developer") == []

    missing_co = text.replace("Acme", "OtherCo")
    errs = validate_cover_letter(missing_co, "Acme", "GenAI Developer")
    assert any("company name missing" in e for e in errs)


def test_validate_cover_letter_rejects_empty_and_too_short():
    assert validate_cover_letter("", "Acme", "Eng") == ["cover letter is empty"]
    short = "I want the Eng job at Acme please hire me."
    errs = validate_cover_letter(short, "Acme", "Eng")
    assert any("too short" in e for e in errs)


def test_cover_letter_prompt_includes_constraints_and_targets():
    prompt = build_cover_letter_prompt(
        profile_json="{}",
        resume_facts=["Company: AIQoD"],
        job_title="GTM Engineer",
        company="CLERA",
        job_description="Build growth engines.",
    )
    assert "NEVER invent" in prompt
    assert "CLERA" in prompt
    assert "GTM Engineer" in prompt
    assert "Company: AIQoD" in prompt
    assert '"cover_letter"' in prompt


# ---------------------------------------------------------------------------
# Per-job generation
# ---------------------------------------------------------------------------

def test_generate_cover_letter_succeeds_when_valid():
    good = {
        "cover_letter": _good_letter("Startup", "GenAI Developer"),
        "emphasis_notes": "Highlighted LangGraph.",
    }
    client = FakeLLMClient(responses=[good])
    letter, notes = generate_cover_letter(
        job_title="GenAI Developer",
        company="Startup",
        job_description="Need LangGraph.",
        profile=FIXTURE_PROFILE,
        llm_client=client,
    )
    assert "Startup" in letter
    assert "LangGraph" in notes
    assert len(client.prompts) == 1


def test_generate_cover_letter_retries_then_succeeds():
    bad = {
        "cover_letter": "Too short and wrong company.",
        "emphasis_notes": "",
    }
    good = {
        "cover_letter": _good_letter("Startup", "GenAI Developer"),
        "emphasis_notes": "Fixed.",
    }
    client = FakeLLMClient(responses=[bad, good])
    letter, _ = generate_cover_letter(
        job_title="GenAI Developer",
        company="Startup",
        job_description="Need GenAI.",
        profile=FIXTURE_PROFILE,
        llm_client=client,
        max_attempts=2,
    )
    assert "Startup" in letter
    assert len(client.prompts) == 2
    assert "FAILED validation" in client.prompts[1]


def test_generate_cover_letter_retries_after_empty_response():
    empty = {"cover_letter": "", "emphasis_notes": ""}
    good = {
        "cover_letter": _good_letter("Startup", "GenAI Developer"),
        "emphasis_notes": "ok",
    }
    client = FakeLLMClient(responses=[empty, good])
    letter, notes = generate_cover_letter(
        job_title="GenAI Developer",
        company="Startup",
        job_description="Need GenAI.",
        profile=FIXTURE_PROFILE,
        llm_client=client,
        max_attempts=2,
    )
    assert "Startup" in letter
    assert notes == "ok"
    assert len(client.prompts) == 2


def test_generate_cover_letter_raises_after_persistent_validation_failure():
    bad = {"cover_letter": "Still broken and short.", "emphasis_notes": ""}
    client = FakeLLMClient(responses=[bad, bad])
    with pytest.raises(CoverLetterError, match="still invalid"):
        generate_cover_letter(
            job_title="X",
            company="Y",
            job_description="Z",
            profile=FIXTURE_PROFILE,
            llm_client=client,
            max_attempts=2,
        )


def test_save_cover_letter_writes_text(tmp_path):
    path = save_cover_letter(
        job_id=7,
        company="Acme Corp",
        title="GTM Engineer",
        letter="Hello Acme",
        output_dir=tmp_path,
    )
    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith("Hello Acme")
    assert "job_7" in path.name
    assert path.suffix == ".txt"


# ---------------------------------------------------------------------------
# Full stage
# ---------------------------------------------------------------------------

def test_run_cover_letter_stage_processes_tailored_jobs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(FIXTURE_PROFILE))
    out_dir = tmp_path / "output" / "cover_letters"
    tailored_dir = tmp_path / "output" / "tailored"
    tailored_dir.mkdir(parents=True)
    tailored_path = tailored_dir / "job_1.md"
    tailored_path.write_text("# Jane Doe\nAIQoD LangGraph experience.\n")

    session_scope = make_temp_sqlite_session_scope()
    with session_scope() as session:
        good = Job(
            url="https://x.com/good",
            title="GenAI Developer",
            company="Startup",
            source="indeed",
            description="LangGraph RAG",
            status="tailored",
            score=8.5,
        )
        low = Job(
            url="https://x.com/scored_only",
            title="Still scored",
            company="B",
            source="indeed",
            description="Whatever",
            status="scored",
            score=9.0,
        )
        session.add_all([good, low])
        session.flush()
        session.add(
            Application(
                job_id=good.id,
                tailored_resume_path=str(tailored_path),
            )
        )

    letter = {
        "cover_letter": _good_letter("Startup", "GenAI Developer"),
        "emphasis_notes": "LangGraph focus.",
    }
    summary = run_cover_letter_stage(
        session_scope=session_scope,
        llm_client=FakeLLMClient(responses=[letter]),
        profile_path=profile_path,
        output_dir=out_dir,
    )

    assert summary["processed"] == 1
    assert summary["cover_letter_ready"] == 1
    assert summary["failed"] == 0
    assert summary["manual_review"] == 0

    with session_scope() as session:
        jobs = {j.url: j for j in session.query(Job).all()}
        assert jobs["https://x.com/good"].status == "cover_letter_ready"
        assert jobs["https://x.com/scored_only"].status == "scored"
        app = (
            session.query(Application)
            .filter_by(job_id=jobs["https://x.com/good"].id)
            .one()
        )
        assert app.cover_letter_path
        assert Path(app.cover_letter_path).exists()


def test_run_cover_letter_stage_passes_tailored_resume_into_prompt(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    profile_path = tmp_path / "config" / "profile.json"
    profile_path.write_text(json.dumps(FIXTURE_PROFILE))
    tailored_path = tmp_path / "resume.md"
    tailored_path.write_text("UNIQUE_TAILORED_MARKER and AIQoD LangGraph.\n")

    session_scope = make_temp_sqlite_session_scope()
    with session_scope() as session:
        job = Job(
            url="https://x.com/t",
            title="GenAI Developer",
            company="Startup",
            source="indeed",
            description="Need GenAI",
            status="tailored",
            score=7.0,
        )
        session.add(job)
        session.flush()
        session.add(
            Application(job_id=job.id, tailored_resume_path=str(tailored_path))
        )

    client = FakeLLMClient(
        responses=[
            {
                "cover_letter": _good_letter("Startup", "GenAI Developer"),
                "emphasis_notes": "ok",
            }
        ]
    )
    run_cover_letter_stage(
        session_scope=session_scope,
        llm_client=client,
        profile_path=profile_path,
        output_dir=tmp_path / "out",
    )
    assert "UNIQUE_TAILORED_MARKER" in client.prompts[0]


def test_run_cover_letter_stage_flags_manual_review_on_persistent_failure(
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
                status="tailored",
                score=9.0,
            )
        )

    bad = {"cover_letter": "nope", "emphasis_notes": ""}
    summary = run_cover_letter_stage(
        session_scope=session_scope,
        llm_client=FakeLLMClient(responses=[bad, bad]),
        profile_path=profile_path,
        output_dir=tmp_path / "output",
    )

    assert summary["processed"] == 1
    assert summary["manual_review"] == 1
    assert summary["cover_letter_ready"] == 0

    with session_scope() as session:
        job = session.query(Job).one()
        assert job.status == "manual_review"
        assert "cover letter validation failed" in (job.reasoning or "").lower()
