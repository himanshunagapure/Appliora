import json
from pathlib import Path

import pytest

from core.resume_parser import (
    ResumeParseError,
    extract_text,
    parse_resume,
    validate_profile,
)

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "config" / "profile.schema.json"

VALID_PROFILE = {
    "contact": {"name": "Jane Doe", "email": "jane@example.com"},
    "summary": "GenAI developer with 1 year of production experience.",
    "skills": ["Python", "LangGraph", "RAG", "Pinecone"],
    "experience": [
        {
            "company": "Acme Corp",
            "title": "GenAI Developer",
            "start_date": "Jan 2024",
            "end_date": "Present",
            "location": "Nagpur, India",
            "achievements": ["Built a multi-agent lead generation system"],
        }
    ],
    "education": [
        {
            "institution": "Some University",
            "degree": "B.E.",
            "field": "Computer Science",
            "marks": "87%",
        }
    ],
    "projects": [
        {
            "name": "LeadGen Bot",
            "technologies": ["Python", "LangGraph"],
            "start_date": None,
            "end_date": None,
            "url": None,
            "achievements": ["Built a multi-agent lead generation system"],
        }
    ],
    "certifications": ["AWS Certified"],
    "resume_facts": [
        "Company: Acme Corp",
        "Title: GenAI Developer",
        "Dates: Jan 2024 - Present",
        "Achievement: Built a multi-agent lead generation system",
        "Project: LeadGen Bot",
        "Grade: 87%",
    ],
}

INVALID_PROFILE = {  # missing required "resume_facts" and "contact.email"
    "contact": {"name": "Jane Doe"},
    "skills": [],
    "experience": [],
    "education": [],
}


class FakeLLMClient:
    """Stands in for llm.client.LLMClient so tests never hit the network."""

    def __init__(self, responses):
        self._responses = list(responses)

    def generate_json(self, prompt, max_retries=None):
        return self._responses.pop(0)


def test_extract_text_from_txt_fixture():
    text = extract_text(FIXTURES / "sample_resume.txt")
    assert "Jane Doe" in text
    assert "GenAI Developer" in text


def test_extract_text_missing_file_raises():
    with pytest.raises(ResumeParseError):
        extract_text(FIXTURES / "does_not_exist.pdf")


def test_extract_text_unsupported_format_raises(tmp_path):
    bad_file = tmp_path / "resume.exe"
    bad_file.write_text("not a resume")
    with pytest.raises(ResumeParseError):
        extract_text(bad_file)


def test_validate_profile_accepts_valid_profile():
    schema = json.loads(SCHEMA_PATH.read_text())
    errors = validate_profile(VALID_PROFILE, schema)
    assert errors == []


def test_validate_profile_flags_missing_required_fields():
    schema = json.loads(SCHEMA_PATH.read_text())
    errors = validate_profile(INVALID_PROFILE, schema)
    assert any("resume_facts" in e for e in errors)


def test_parse_resume_end_to_end_with_valid_llm_response(tmp_path):
    fake_client = FakeLLMClient(responses=[VALID_PROFILE])
    output_path = tmp_path / "profile.json"

    profile = parse_resume(
        resume_path=FIXTURES / "sample_resume.txt",
        output_path=output_path,
        llm_client=fake_client,
    )

    assert profile["contact"]["name"] == "Jane Doe"
    assert output_path.exists()
    saved = json.loads(output_path.read_text())
    assert saved == VALID_PROFILE


def test_parse_resume_retries_then_succeeds_after_invalid_first_attempt(tmp_path):
    fake_client = FakeLLMClient(responses=[INVALID_PROFILE, VALID_PROFILE])
    output_path = tmp_path / "profile.json"

    profile = parse_resume(
        resume_path=FIXTURES / "sample_resume.txt",
        output_path=output_path,
        llm_client=fake_client,
        max_attempts=2,
    )

    assert profile == VALID_PROFILE


def test_parse_resume_raises_after_max_attempts_all_invalid(tmp_path):
    fake_client = FakeLLMClient(responses=[INVALID_PROFILE, INVALID_PROFILE])
    output_path = tmp_path / "profile.json"

    with pytest.raises(ResumeParseError):
        parse_resume(
            resume_path=FIXTURES / "sample_resume.txt",
            output_path=output_path,
            llm_client=fake_client,
            max_attempts=2,
        )
