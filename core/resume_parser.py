"""
Phase 2 — Resume Parsing (FR1)

Turns a PDF/DOCX/TXT resume into a validated, structured config/profile.json
that every later stage (scoring, tailoring, cover letters) reads from.

Public entry point: parse_resume()
"""

import json
from pathlib import Path
from typing import Any

import jsonschema
from docx import Document
from pypdf import PdfReader

from config.settings import get_settings
from llm.client import LLMClient, LLMError
from utils.logger import get_logger

logger = get_logger("jobapplypro.resume_parser")


class ResumeParseError(RuntimeError):
    """Raised when a resume can't be read or the extracted profile fails validation."""


# ---------------------------------------------------------------------------
# 1. Text extraction
# ---------------------------------------------------------------------------

def extract_text(resume_path: str | Path) -> str:
    """Extract raw text from a PDF, DOCX, or plain TXT resume."""
    path = Path(resume_path)
    if not path.exists():
        raise ResumeParseError(f"Resume file not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(path)
    elif suffix == ".docx":
        return _extract_docx_text(path)
    elif suffix == ".txt":
        return path.read_text(encoding="utf-8")
    else:
        raise ResumeParseError(
            f"Unsupported resume format '{suffix}'. Use PDF, DOCX, or TXT."
        )


def _extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n".join(pages).strip()
    if not text:
        raise ResumeParseError(
            f"No extractable text found in {path.name}. "
            "It may be a scanned/image-only PDF — try exporting a text-based PDF instead."
        )
    return text


def _extract_docx_text(path: Path) -> str:
    document = Document(str(path))
    paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
    text = "\n".join(paragraphs).strip()
    if not text:
        raise ResumeParseError(f"No extractable text found in {path.name}.")
    return text


# ---------------------------------------------------------------------------
# 2. Prompt construction
# ---------------------------------------------------------------------------

def _load_schema() -> dict[str, Any]:
    schema_path = get_settings().profile_schema_path
    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)


def build_extraction_prompt(resume_text: str, schema: dict[str, Any]) -> str:
    return f"""You are extracting structured data from a resume. Read the resume
text below and output a single JSON object that matches this JSON Schema exactly:

{json.dumps(schema, indent=2)}

Critical rules:
1. Output ONLY the JSON object. No markdown fences, no commentary, no explanation.
2. Do not invent, infer, or embellish anything not present in the resume text.
   If a field is genuinely absent, use null (or an empty array for list fields).
3. Always populate "projects" (use [] if none) and education[].marks when a grade,
   percentage, CGPA, or score is stated (e.g. "87%", "8.5 CGPA"); otherwise null.
4. For "resume_facts": extract atomic, verbatim facts as short strings, e.g.
   "Company: Acme Corp", "Title: Senior Backend Engineer", "Dates: Jan 2022 - Present",
   "Achievement: Reduced API latency by 40%", "Project: Orkestrix",
   "Project Technologies: Python, LangGraph", "Grade: 87%". These will be used later
   to make sure no downstream AI rewriting ever contradicts what's actually true about
   this candidate, so be exhaustive and precise — pull every company, title, date range,
   project, grade/marks, and quantified achievement into this list.
5. Keep "achievements" bullet text close to the original wording; don't summarize it away.

Resume text:
---
{resume_text}
---

JSON object:"""


# ---------------------------------------------------------------------------
# 3. Validation
# ---------------------------------------------------------------------------

def validate_profile(profile: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Returns a list of validation error messages (empty list = valid)."""
    validator = jsonschema.Draft7Validator(schema)
    return [f"{'/'.join(str(p) for p in e.path)}: {e.message}" for e in validator.iter_errors(profile)]


# ---------------------------------------------------------------------------
# 4. Save
# ---------------------------------------------------------------------------

def save_profile(profile: dict[str, Any], output_path: str | Path | None = None) -> Path:
    output_path = Path(output_path or get_settings().profile_output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)
    logger.info("Saved profile to %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# 5. Orchestration
# ---------------------------------------------------------------------------

def parse_resume(
    resume_path: str | Path,
    output_path: str | Path | None = None,
    llm_client: LLMClient | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """
    Full pipeline: extract text -> prompt LLM -> validate -> save.
    Retries once with validation errors fed back into the prompt if the
    first attempt doesn't pass schema validation.
    """
    client = llm_client or LLMClient()
    schema = _load_schema()

    logger.info("Extracting text from %s", resume_path)
    resume_text = extract_text(resume_path)

    prompt = build_extraction_prompt(resume_text, schema)
    errors: list[str] = []

    for attempt in range(1, max_attempts + 1):
        logger.info("Requesting structured extraction (attempt %d/%d)", attempt, max_attempts)
        try:
            profile = client.generate_json(prompt)
        except LLMError as exc:
            raise ResumeParseError(f"LLM extraction failed: {exc}") from exc

        errors = validate_profile(profile, schema)
        if not errors:
            save_profile(profile, output_path)
            logger.info(
                "Profile extracted successfully: %d skills, %d experience entries, %d resume_facts",
                len(profile.get("skills", [])),
                len(profile.get("experience", [])),
                len(profile.get("resume_facts", [])),
            )
            return profile

        logger.warning("Profile failed schema validation (attempt %d): %s", attempt, errors)
        prompt = (
            f"{prompt}\n\nYour previous JSON output failed schema validation with these "
            f"errors:\n{json.dumps(errors, indent=2)}\n\nFix them and output the corrected "
            f"JSON object only."
        )

    raise ResumeParseError(
        f"Profile failed schema validation after {max_attempts} attempts: {errors}"
    )
