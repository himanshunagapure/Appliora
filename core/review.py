"""
Phase 8 — Review & submission gate (FR6 follow-on).

Dry-run apply lands jobs in status="apply_pending". A human reviews the
screenshot + tailored materials, then approve or reject. Real submit
(`python cli.py run --stage apply --submit --job-id`) only runs after
explicit approval.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO

from db.models import Application, Job
from db.session import get_db_session
from utils.logger import get_logger, log_transition

logger = get_logger("jobapplypro.review")

REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
PENDING_JOB_STATUS = "apply_pending"
REJECTED_JOB_STATUS = "skipped"

EXCERPT_LINES = 18
EXCERPT_CHARS = 1200


class ReviewError(RuntimeError):
    """Raised for invalid review / approval actions."""


@dataclass
class ReviewItem:
    """One apply_pending job plus its application materials."""

    job_id: int
    title: str
    company: str
    url: str
    score: float | None
    status: str
    review_decision: str | None
    screenshot_path: str | None
    tailored_resume_path: str | None
    cover_letter_path: str | None
    submitted_at: datetime | None
    reasoning: str | None

    @property
    def is_approved(self) -> bool:
        return self.review_decision == REVIEW_APPROVED


def is_application_approved(application: Application | None) -> bool:
    return bool(
        application is not None and application.review_decision == REVIEW_APPROVED
    )


def _file_status(path: str | None) -> str:
    if not path:
        return "missing"
    p = Path(path)
    if not p.exists():
        return f"missing ({path})"
    size = p.stat().st_size
    if size >= 1024 * 1024:
        human = f"{size / (1024 * 1024):.1f} MB"
    elif size >= 1024:
        human = f"{size / 1024:.0f} KB"
    else:
        human = f"{size} B"
    return f"ok ({human})"


def _excerpt(path: str | None) -> str:
    if not path:
        return "(none)"
    p = Path(path)
    if not p.exists():
        return f"(file not found: {path})"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(unreadable: {exc})"
    if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".docx", ".doc"}:
        return f"(binary {p.suffix} — open the file to inspect)"
    lines = text.splitlines()
    snippet = "\n".join(lines[:EXCERPT_LINES])
    if len(snippet) > EXCERPT_CHARS:
        snippet = snippet[:EXCERPT_CHARS].rstrip() + "…"
    if len(lines) > EXCERPT_LINES:
        snippet += f"\n… ({len(lines) - EXCERPT_LINES} more lines)"
    return snippet or "(empty file)"


def _last_apply_note(reasoning: str | None) -> str:
    if not reasoning:
        return "(none)"
    lines = [ln.strip() for ln in reasoning.splitlines() if ln.strip()]
    for line in reversed(lines):
        if line.lower().startswith("apply(") or line.lower().startswith("review:"):
            return line
    return lines[-1] if lines else "(none)"


def format_review_card(item: ReviewItem) -> str:
    decision = item.review_decision or "pending"
    score = f"{item.score:.1f}" if item.score is not None else "—"
    lines = [
        f"Job {item.job_id}  {item.company} — {item.title}",
        f"  status={item.status}  review={decision}  score={score}",
        f"  url: {item.url}",
        f"  screenshot: {item.screenshot_path or '(none)'}  [{_file_status(item.screenshot_path)}]",
        f"  resume:     {item.tailored_resume_path or '(none)'}  [{_file_status(item.tailored_resume_path)}]",
        f"  cover:      {item.cover_letter_path or '(none)'}  [{_file_status(item.cover_letter_path)}]",
        f"  last note:  {_last_apply_note(item.reasoning)}",
        "",
        "  --- resume excerpt ---",
        _indent(_excerpt(item.tailored_resume_path)),
        "",
        "  --- cover letter excerpt ---",
        _indent(_excerpt(item.cover_letter_path)),
    ]
    return "\n".join(lines)


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line if line else prefix.rstrip() for line in text.splitlines())


def _item_from_row(job: Job, application: Application | None) -> ReviewItem:
    return ReviewItem(
        job_id=job.id,
        title=job.title,
        company=job.company,
        url=job.url,
        score=job.score,
        status=job.status,
        review_decision=application.review_decision if application else None,
        screenshot_path=application.screenshot_path if application else None,
        tailored_resume_path=application.tailored_resume_path if application else None,
        cover_letter_path=application.cover_letter_path if application else None,
        submitted_at=application.submitted_at if application else None,
        reasoning=job.reasoning,
    )


def list_pending_reviews(
    *,
    job_id: int | None = None,
    session_scope: Callable | None = None,
) -> list[ReviewItem]:
    """Return apply_pending jobs (optionally one id), newest score first."""
    session_scope = session_scope or get_db_session
    with session_scope() as session:
        query = session.query(Job).filter(Job.status == PENDING_JOB_STATUS)
        if job_id is not None:
            query = query.filter(Job.id == job_id)
        jobs = query.order_by(Job.score.desc(), Job.id.asc()).all()
        items: list[ReviewItem] = []
        for job in jobs:
            application = (
                session.query(Application)
                .filter(Application.job_id == job.id)
                .one_or_none()
            )
            items.append(_item_from_row(job, application))
        return items


def _load_pending_job(session, job_id: int) -> tuple[Job, Application]:
    job = session.query(Job).filter(Job.id == job_id).one_or_none()
    if job is None:
        raise ReviewError(f"job {job_id} not found")
    if job.status != PENDING_JOB_STATUS:
        raise ReviewError(
            f"job {job_id} status is {job.status!r}, expected {PENDING_JOB_STATUS!r}"
        )
    application = (
        session.query(Application).filter(Application.job_id == job.id).one_or_none()
    )
    if application is None:
        application = Application(job_id=job.id)
        session.add(application)
        session.flush()
    return job, application


def _append_note(job: Job, note: str) -> None:
    job.reasoning = f"{job.reasoning}\n{note}" if job.reasoning else note


def approve_job(
    job_id: int,
    *,
    session_scope: Callable | None = None,
) -> ReviewItem:
    """Mark an apply_pending job as approved (eligible for --submit)."""
    session_scope = session_scope or get_db_session
    with session_scope() as session:
        job, application = _load_pending_job(session, job_id)
        if not application.screenshot_path or not Path(
            application.screenshot_path
        ).exists():
            logger.warning(
                "Approving job %d without a screenshot on disk — inspect the "
                "listing carefully before --submit",
                job.id,
            )
        if not application.tailored_resume_path or not Path(
            application.tailored_resume_path
        ).exists():
            logger.warning(
                "Approving job %d without a tailored resume file",
                job.id,
            )
        application.review_decision = REVIEW_APPROVED
        application.reviewed_at = datetime.now(timezone.utc)
        _append_note(job, "Review: approved")
        log_transition(
            logger,
            str(job.id),
            "review",
            PENDING_JOB_STATUS,
            PENDING_JOB_STATUS,
            "approved for submit",
        )
        session.flush()
        logger.info("Approved job %d (%s — %s)", job.id, job.company, job.title)
        return _item_from_row(job, application)


def reject_job(
    job_id: int,
    *,
    reason: str | None = None,
    session_scope: Callable | None = None,
) -> ReviewItem:
    """Reject an apply_pending job — will not be submitted."""
    session_scope = session_scope or get_db_session
    with session_scope() as session:
        job, application = _load_pending_job(session, job_id)
        previous = job.status
        application.review_decision = REVIEW_REJECTED
        application.reviewed_at = datetime.now(timezone.utc)
        job.status = REJECTED_JOB_STATUS
        detail = f"Review: rejected" + (f" ({reason})" if reason else "")
        _append_note(job, detail)
        log_transition(
            logger,
            str(job.id),
            "review",
            previous,
            REJECTED_JOB_STATUS,
            reason or "rejected",
        )
        session.flush()
        logger.info("Rejected job %d (%s — %s)", job.id, job.company, job.title)
        return _item_from_row(job, application)


def open_screenshot(path: str | None) -> bool:
    """Open a screenshot in the desktop viewer if possible."""
    if not path:
        return False
    target = Path(path)
    if not target.exists():
        logger.warning("Screenshot not found: %s", path)
        return False
    opener = shutil.which("xdg-open") or shutil.which("open")
    if not opener:
        logger.warning("No xdg-open/open available to view %s", path)
        return False
    try:
        subprocess.Popen(
            [opener, str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError as exc:
        logger.warning("Failed to open screenshot %s: %s", path, exc)
        return False


def run_interactive_review(
    *,
    job_id: int | None = None,
    open_screenshots: bool = False,
    session_scope: Callable | None = None,
    input_fn: Callable[[str], str] = input,
    output: TextIO | None = None,
) -> dict[str, int]:
    """
    Walk apply_pending jobs and prompt a/r/s/q (approve / reject / skip / quit).
    """
    import sys

    out = output or sys.stdout
    items = list_pending_reviews(job_id=job_id, session_scope=session_scope)
    summary = {"shown": 0, "approved": 0, "rejected": 0, "skipped": 0}
    if not items:
        print("No apply_pending jobs to review.", file=out)
        return summary

    total = len(items)
    for i, item in enumerate(items, 1):
        print("\n" + "=" * 72, file=out)
        print(f"[{i}/{total}]", file=out)
        print(format_review_card(item), file=out)
        if open_screenshots:
            open_screenshot(item.screenshot_path)
        summary["shown"] += 1
        while True:
            try:
                choice = input_fn("Approve / Reject / Skip / Quit [a/r/s/q]: ").strip().lower()
            except EOFError:
                choice = "q"
            if choice in {"a", "approve", "y", "yes"}:
                approve_job(item.job_id, session_scope=session_scope)
                summary["approved"] += 1
                print(f"Approved job {item.job_id}.", file=out)
                break
            if choice in {"r", "reject", "n", "no"}:
                reject_job(item.job_id, session_scope=session_scope)
                summary["rejected"] += 1
                print(f"Rejected job {item.job_id} → skipped.", file=out)
                break
            if choice in {"s", "skip", ""}:
                summary["skipped"] += 1
                print(f"Skipped job {item.job_id}.", file=out)
                break
            if choice in {"q", "quit"}:
                print("Quit review.", file=out)
                return summary
            print("Please enter a, r, s, or q.", file=out)
    return summary
