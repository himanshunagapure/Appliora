"""
Phase 6 — Cover Letter Generation (FR5)

For every job in status="tailored": generate a short job-specific cover letter,
validate it references the company + role and is non-empty, retry once on
failure, then either:
  - save the letter + mark status="cover_letter_ready", or
  - flag status="manual_review" / "failed" on persistent problems.

Public entry point: run_cover_letter_stage()
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config.settings import get_settings
from core.tailor import load_profile
from db.models import Application, Job, RunLog
from db.session import get_db_session
from llm.client import LLMClient, LLMError
from llm.prompts.cover_letter_prompt import build_cover_letter_prompt
from utils.logger import get_logger

logger = get_logger("jobapplypro.cover_letter")


class CoverLetterError(RuntimeError):
    """Raised when a single job's cover letter can't be generated (caught per-job)."""


# ---------------------------------------------------------------------------
# 1. Validation
# ---------------------------------------------------------------------------

def validate_cover_letter(
    text: str,
    company: str,
    job_title: str,
    *,
    min_words: int = 80,
    max_words: int = 500,
) -> list[str]:
    """
    Return a list of validation problems. Empty list means the letter is OK.
    FR5 requires a short letter that references the company and role.
    """
    errors: list[str] = []
    body = (text or "").strip()
    if not body:
        errors.append("cover letter is empty")
        return errors

    lower = body.lower()
    company_key = (company or "").strip()
    title_key = (job_title or "").strip()

    if company_key and company_key.lower() not in lower:
        errors.append(f"company name missing as substring: {company_key}")

    if title_key and title_key.lower() not in lower:
        errors.append(f"job title missing as substring: {title_key}")

    words = re.findall(r"\b\w+\b", body)
    n_words = len(words)
    if n_words < min_words:
        errors.append(f"cover letter too short ({n_words} words; need >={min_words})")
    if n_words > max_words:
        errors.append(f"cover letter too long ({n_words} words; need <={max_words})")

    return errors


# ---------------------------------------------------------------------------
# 2. Output helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug or "job")[:max_len]


def save_cover_letter(
    job_id: int,
    company: str,
    title: str,
    letter: str,
    output_dir: str | Path | None = None,
) -> Path:
    settings = get_settings()
    base = Path(output_dir or settings.cover_letter_output_dir)
    base.mkdir(parents=True, exist_ok=True)
    filename = f"job_{job_id}_{_slugify(company)}_{_slugify(title)}.txt"
    path = base / filename
    path.write_text(letter.strip() + "\n", encoding="utf-8")
    return path


def _truncate(text: str, max_chars: int = 8000) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[truncated]"


def _read_tailored_resume(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        logger.warning("Tailored resume path missing on disk: %s", path)
        return None
    try:
        from automation.resume_upload import read_resume_text

        return _truncate(read_resume_text(p), max_chars=6000)
    except Exception as exc:
        logger.warning("Could not read tailored resume %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# 3. Per-job generation
# ---------------------------------------------------------------------------

def generate_cover_letter(
    job_title: str,
    company: str,
    job_description: str,
    profile: dict[str, Any],
    llm_client: LLMClient,
    tailored_resume: str | None = None,
    max_attempts: int = 2,
) -> tuple[str, str]:
    """
    Returns (cover_letter_text, emphasis_notes).
    Raises CoverLetterError if the LLM fails or validation still fails after retries.
    """
    resume_facts = [str(f) for f in profile.get("resume_facts", []) if str(f).strip()]
    if not resume_facts:
        raise CoverLetterError(
            "profile.json has no resume_facts — refusing to write a cover letter."
        )

    profile_for_prompt = {k: v for k, v in profile.items() if k != "resume_facts"}
    profile_json = json.dumps(profile_for_prompt, indent=2)
    job_description = _truncate(job_description or "")

    validation_errors: list[str] = []
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        prompt = build_cover_letter_prompt(
            profile_json=profile_json,
            resume_facts=resume_facts,
            job_title=job_title,
            company=company,
            job_description=job_description,
            tailored_resume=tailored_resume,
            validation_errors=validation_errors or None,
        )
        try:
            result = llm_client.generate_json(prompt)
        except LLMError as exc:
            last_error = CoverLetterError(f"LLM cover letter failed: {exc}")
            logger.warning(
                "LLM call failed on cover letter attempt %d/%d: %s",
                attempt,
                max_attempts,
                exc,
            )
            continue

        letter = str(result.get("cover_letter") or "").strip()
        notes = str(result.get("emphasis_notes") or "").strip()
        if not letter:
            last_error = CoverLetterError(
                f"LLM returned empty cover_letter: {result}"
            )
            logger.warning(
                "Empty cover_letter on attempt %d/%d", attempt, max_attempts
            )
            validation_errors = ["cover letter is empty"]
            continue

        validation_errors = validate_cover_letter(letter, company, job_title)
        if not validation_errors:
            return letter, notes

        last_error = CoverLetterError(
            f"cover letter still invalid after {max_attempts} attempts: "
            f"{validation_errors}"
        )
        logger.warning(
            "Cover letter failed validation (attempt %d/%d): %s",
            attempt,
            max_attempts,
            validation_errors,
        )

    if last_error is not None:
        raise last_error
    raise CoverLetterError(
        f"cover letter still invalid after {max_attempts} attempts: "
        f"{validation_errors}"
    )


# ---------------------------------------------------------------------------
# 4. Stage orchestration
# ---------------------------------------------------------------------------

def run_cover_letter_stage(
    session_scope: Callable | None = None,
    llm_client: LLMClient | None = None,
    profile_path: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, int]:
    """
    Writes a cover letter for every job in status="tailored".
    Commits per job so a crash mid-batch does not lose earlier successes.
    """
    settings = get_settings()
    session_scope = session_scope or get_db_session
    llm_client = llm_client or LLMClient()
    try:
        profile = load_profile(profile_path)
    except Exception as exc:
        # load_profile raises TailoringError; surface as CoverLetterError for this stage.
        raise CoverLetterError(str(exc)) from exc
    output_dir = Path(output_dir or settings.cover_letter_output_dir)

    summary = {
        "processed": 0,
        "cover_letter_ready": 0,
        "manual_review": 0,
        "failed": 0,
    }
    error_messages: list[str] = []

    with session_scope() as session:
        run_log = RunLog(stage="cover_letter", jobs_processed=0)
        session.add(run_log)
        session.flush()

        eligible = (
            session.query(Job)
            .filter(Job.status == "tailored")
            .order_by(Job.score.desc(), Job.id.asc())
            .all()
        )
        logger.info("Generating cover letters for %d tailored job(s).", len(eligible))

        for job in eligible:
            summary["processed"] += 1
            try:
                application = (
                    session.query(Application)
                    .filter(Application.job_id == job.id)
                    .one_or_none()
                )
                tailored_text = _read_tailored_resume(
                    application.tailored_resume_path if application else None
                )

                letter, notes = generate_cover_letter(
                    job_title=job.title,
                    company=job.company,
                    job_description=job.description or "",
                    profile=profile,
                    llm_client=llm_client,
                    tailored_resume=tailored_text,
                )
                try:
                    path = save_cover_letter(
                        job_id=job.id,
                        company=job.company,
                        title=job.title,
                        letter=letter,
                        output_dir=output_dir,
                    )
                except OSError as exc:
                    raise CoverLetterError(
                        f"Failed to write cover letter: {exc}"
                    ) from exc

                if application is None:
                    application = Application(job_id=job.id)
                    session.add(application)
                application.cover_letter_path = str(path)

                job.status = "cover_letter_ready"
                note = f"Cover letter saved to {path}"
                if notes:
                    note += f" | {notes}"
                job.reasoning = (
                    f"{job.reasoning}\n{note}" if job.reasoning else note
                )
                summary["cover_letter_ready"] += 1
                logger.info(
                    "Cover letter for job %d (%s at %s) -> %s",
                    job.id,
                    job.title,
                    job.company,
                    path,
                )
            except CoverLetterError as exc:
                if "still invalid" in str(exc):
                    job.status = "manual_review"
                    job.reasoning = f"Cover letter validation failed: {exc}"
                    summary["manual_review"] += 1
                    logger.error(
                        "Job %d flagged manual_review after cover letter failure: %s",
                        job.id,
                        exc,
                    )
                else:
                    job.status = "failed"
                    job.reasoning = f"Cover letter failed: {exc}"
                    summary["failed"] += 1
                    logger.error(
                        "Failed cover letter for job %d (%s): %s",
                        job.id,
                        job.title,
                        exc,
                    )
                error_messages.append(f"job {job.id}: {exc}")
            except Exception as exc:
                job.status = "failed"
                job.reasoning = f"Cover letter failed: {exc}"
                summary["failed"] += 1
                error_messages.append(f"job {job.id}: {exc}")
                logger.error(
                    "Unexpected error writing cover letter for job %d (%s): %s",
                    job.id,
                    job.title,
                    exc,
                    exc_info=True,
                )

            session.commit()

        run_log.jobs_processed = summary["processed"]
        run_log.finished_at = datetime.now(timezone.utc)
        run_log.errors = "\n".join(error_messages) if error_messages else None
        session.commit()

    if summary["processed"] == 0:
        logger.info("No tailored jobs — nothing to write cover letters for.")

    logger.info("Cover letter stage complete: %s", summary)
    return summary
