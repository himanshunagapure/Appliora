"""
Phase 5 — Resume Tailoring (FR4)

For every job in status="scored" with score >= score_threshold: generate a
job-specific markdown resume, validate it preserves every resume_fact, retry
once on failure, then either:
  - save the resume + mark status="tailored", or
  - flag status="manual_review" if facts are still missing after retry.

Public entry point: run_tailoring_stage()
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config.settings import get_settings
from db.models import Application, Job, RunLog
from db.session import get_db_session
from llm.client import LLMClient, LLMError
from llm.prompts.tailor_prompt import build_tailor_prompt
from utils.logger import get_logger

logger = get_logger("jobapplypro.tailor")


class TailoringError(RuntimeError):
    """Raised when a single job can't be tailored (caught per-job by the stage runner)."""


# ---------------------------------------------------------------------------
# 1. Profile + fact validation
# ---------------------------------------------------------------------------

def load_profile(profile_path: str | Path | None = None) -> dict[str, Any]:
    settings = get_settings()
    path = Path(profile_path or settings.profile_output_path)
    if not path.exists():
        raise TailoringError(
            f"No profile found at {path}. Run `python cli.py init --resume <path>` first."
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fact_payload(fact: str) -> str:
    """
    Extract the protected content from a resume_fact.
    "Company: Acme Corp" -> "Acme Corp"; facts without a label stay as-is.
    """
    if ": " in fact:
        return fact.split(": ", 1)[1].strip()
    return fact.strip()


def find_missing_resume_facts(tailored_text: str, resume_facts: list[str]) -> list[str]:
    """
    Return resume_facts whose protected content does not appear verbatim in
    the tailored resume. This is the hard FR4 constraint.
    """
    missing: list[str] = []
    for fact in resume_facts:
        payload = fact_payload(fact)
        if not payload:
            continue
        if payload not in tailored_text:
            missing.append(fact)
    return missing


# ---------------------------------------------------------------------------
# 2. Output helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug or "job")[:max_len]


def save_tailored_resume(
    job_id: int,
    company: str,
    title: str,
    markdown: str,
    output_dir: str | Path | None = None,
) -> Path:
    settings = get_settings()
    base = Path(output_dir or settings.tailored_output_dir)
    base.mkdir(parents=True, exist_ok=True)
    filename = f"job_{job_id}_{_slugify(company)}_{_slugify(title)}.md"
    path = base / filename
    path.write_text(markdown.strip() + "\n", encoding="utf-8")
    return path


def _truncate(text: str, max_chars: int = 8000) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[truncated]"


# ---------------------------------------------------------------------------
# 3. Per-job tailoring
# ---------------------------------------------------------------------------

def tailor_job(
    job_title: str,
    company: str,
    job_description: str,
    profile: dict[str, Any],
    llm_client: LLMClient,
    max_attempts: int = 2,
) -> tuple[str, str]:
    """
    Returns (tailored_markdown, emphasis_notes).
    Raises TailoringError if the LLM fails or facts still missing after retries.
    """
    resume_facts = [str(f) for f in profile.get("resume_facts", []) if str(f).strip()]
    if not resume_facts:
        raise TailoringError("profile.json has no resume_facts — refusing to tailor.")

    # Keep full profile (minus resume_facts list) so contact/education/projects
    # remain available for the resume body. Facts are passed separately.
    profile_for_prompt = {k: v for k, v in profile.items() if k != "resume_facts"}
    profile_json = json.dumps(profile_for_prompt, indent=2)
    job_description = _truncate(job_description or "")

    missing: list[str] = []
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        prompt = build_tailor_prompt(
            profile_json=profile_json,
            resume_facts=resume_facts,
            job_title=job_title,
            company=company,
            job_description=job_description,
            validation_errors=missing or None,
        )
        try:
            result = llm_client.generate_json(prompt)
        except LLMError as exc:
            last_error = TailoringError(f"LLM tailoring failed: {exc}")
            logger.warning(
                "LLM call failed on tailor attempt %d/%d: %s",
                attempt,
                max_attempts,
                exc,
            )
            continue

        tailored = str(result.get("tailored_resume") or "").strip()
        notes = str(result.get("emphasis_notes") or "").strip()
        if not tailored:
            # Retry — do not abort the whole job on a single empty response.
            last_error = TailoringError(f"LLM returned empty tailored_resume: {result}")
            logger.warning(
                "Empty tailored_resume on attempt %d/%d", attempt, max_attempts
            )
            continue

        missing = find_missing_resume_facts(tailored, resume_facts)
        if not missing:
            return tailored, notes

        last_error = TailoringError(
            f"resume_facts still missing after {max_attempts} attempts: {missing}"
        )
        logger.warning(
            "Tailored resume failed fact check (attempt %d/%d): %d missing fact(s)",
            attempt,
            max_attempts,
            len(missing),
        )

    if last_error is not None:
        raise last_error
    raise TailoringError(
        f"resume_facts still missing after {max_attempts} attempts: {missing}"
    )


# ---------------------------------------------------------------------------
# 4. Stage orchestration
# ---------------------------------------------------------------------------

def run_tailoring_stage(
    session_scope: Callable | None = None,
    llm_client: LLMClient | None = None,
    profile_path: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, int]:
    """
    Tailors every scored job at/above the score threshold.
    Commits per job so a crash mid-batch does not lose earlier successes.
    """
    settings = get_settings()
    session_scope = session_scope or get_db_session
    llm_client = llm_client or LLMClient()
    profile = load_profile(profile_path)
    output_dir = Path(output_dir or settings.tailored_output_dir)

    summary = {
        "processed": 0,
        "tailored": 0,
        "manual_review": 0,
        "failed": 0,
    }
    error_messages: list[str] = []

    with session_scope() as session:
        run_log = RunLog(stage="tailor", jobs_processed=0)
        session.add(run_log)
        session.flush()

        eligible = (
            session.query(Job)
            .filter(
                Job.status == "scored",
                Job.score.isnot(None),
                Job.score >= settings.score_threshold,
            )
            .order_by(Job.score.desc())
            .all()
        )
        logger.info(
            "Tailoring %d scored job(s) at/above threshold %.1f.",
            len(eligible),
            settings.score_threshold,
        )

        for job in eligible:
            summary["processed"] += 1
            try:
                markdown, notes = tailor_job(
                    job_title=job.title,
                    company=job.company,
                    job_description=job.description or "",
                    profile=profile,
                    llm_client=llm_client,
                )
                try:
                    path = save_tailored_resume(
                        job_id=job.id,
                        company=job.company,
                        title=job.title,
                        markdown=markdown,
                        output_dir=output_dir,
                    )
                except OSError as exc:
                    raise TailoringError(
                        f"Failed to write tailored resume: {exc}"
                    ) from exc

                application = (
                    session.query(Application)
                    .filter(Application.job_id == job.id)
                    .one_or_none()
                )
                if application is None:
                    application = Application(job_id=job.id)
                    session.add(application)
                application.tailored_resume_path = str(path)

                job.status = "tailored"
                tailor_note = f"Tailored resume saved to {path}"
                if notes:
                    tailor_note += f" | {notes}"
                job.reasoning = (
                    f"{job.reasoning}\n{tailor_note}" if job.reasoning else tailor_note
                )
                summary["tailored"] += 1
                logger.info(
                    "Tailored job %d (%s at %s) -> %s",
                    job.id,
                    job.title,
                    job.company,
                    path,
                )
            except TailoringError as exc:
                if "resume_facts still missing" in str(exc):
                    job.status = "manual_review"
                    job.reasoning = f"Tailoring fact-check failed: {exc}"
                    summary["manual_review"] += 1
                    logger.error(
                        "Job %d flagged manual_review after fact-check failure: %s",
                        job.id,
                        exc,
                    )
                else:
                    job.status = "failed"
                    job.reasoning = f"Tailoring failed: {exc}"
                    summary["failed"] += 1
                    logger.error(
                        "Failed to tailor job %d (%s): %s", job.id, job.title, exc
                    )
                error_messages.append(f"job {job.id}: {exc}")
            except Exception as exc:
                # Isolate unexpected errors so one bad job cannot abort the batch.
                job.status = "failed"
                job.reasoning = f"Tailoring failed: {exc}"
                summary["failed"] += 1
                error_messages.append(f"job {job.id}: {exc}")
                logger.error(
                    "Unexpected error tailoring job %d (%s): %s",
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
        logger.info(
            "No scored jobs at/above threshold %.1f — nothing to tailor.",
            settings.score_threshold,
        )

    logger.info("Tailoring stage complete: %s", summary)
    return summary
