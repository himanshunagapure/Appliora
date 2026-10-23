"""
Phase 9 — Pipeline status reporting.

`python cli.py status` shows live counts per job status so you can see
where work is piled up (discovered vs stuck in manual_review, etc.).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Callable

from sqlalchemy import func

from db.models import Application, Job, RunLog
from db.session import get_db_session
from utils.logger import get_logger

logger = get_logger("jobapplypro.status")

# Display order for the common happy-path statuses, then the rest.
STATUS_ORDER = (
    "discovered",
    "scored",
    "tailored",
    "cover_letter_ready",
    "apply_pending",
    "applied",
    "manual_review",
    "failed",
    "skipped",
)


def get_status_counts(
    *,
    session_scope: Callable | None = None,
) -> dict[str, Any]:
    """Return job counts by status plus a few operational extras."""
    session_scope = session_scope or get_db_session
    with session_scope() as session:
        rows = (
            session.query(Job.status, func.count(Job.id))
            .group_by(Job.status)
            .all()
        )
        by_status: dict[str, int] = {str(status): int(count) for status, count in rows}

        ordered: OrderedDict[str, int] = OrderedDict()
        for key in STATUS_ORDER:
            ordered[key] = by_status.get(key, 0)
        for key, count in sorted(by_status.items()):
            if key not in ordered:
                ordered[key] = count

        total = sum(ordered.values())

        threshold = 6.0
        try:
            from config.settings import get_settings

            threshold = float(get_settings().score_threshold)
        except Exception:
            pass

        scored_above = (
            session.query(func.count(Job.id))
            .filter(
                Job.status == "scored",
                Job.score.isnot(None),
                Job.score >= threshold,
            )
            .scalar()
        )

        approved_pending = (
            session.query(func.count(Application.id))
            .join(Job, Job.id == Application.job_id)
            .filter(
                Job.status == "apply_pending",
                Application.review_decision == "approved",
            )
            .scalar()
        )

        last_runs = (
            session.query(RunLog)
            .order_by(RunLog.id.desc())
            .limit(8)
            .all()
        )
        recent = [
            {
                "id": r.id,
                "stage": r.stage,
                "jobs_processed": r.jobs_processed,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "has_errors": bool(r.errors),
            }
            for r in last_runs
        ]

        return {
            "by_status": dict(ordered),
            "total": total,
            "score_threshold": threshold,
            "scored_above_threshold": int(scored_above or 0),
            "apply_pending_approved": int(approved_pending or 0),
            "recent_runs": recent,
        }


def list_jobs(
    *,
    session_scope: Callable | None = None,
    status: str | None = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    """Recent jobs for the dashboard table (newest first)."""
    session_scope = session_scope or get_db_session
    limit = max(1, min(int(limit), 200))
    with session_scope() as session:
        q = session.query(Job).order_by(Job.id.desc())
        if status:
            q = q.filter(Job.status == status)
        rows = q.limit(limit).all()
        return [
            {
                "id": j.id,
                "title": j.title,
                "company": j.company,
                "source": j.source,
                "status": j.status,
                "score": j.score,
                "url": j.url,
            }
            for j in rows
        ]


def format_status_report(data: dict[str, Any] | None = None, **kwargs) -> str:
    """Human-readable status block for the CLI."""
    data = data or get_status_counts(**kwargs)
    lines = [
        "JobApplyPro status",
        f"  Total jobs: {data['total']}",
        "",
        "  By status:",
    ]
    for status, count in data["by_status"].items():
        # Hide zero rows except the primary pipeline statuses.
        if count == 0 and status not in STATUS_ORDER[:6]:
            continue
        lines.append(f"    {status:22s} {count}")

    lines.append("")
    lines.append(
        f"  Scored ≥ {data['score_threshold']} (still status=scored): "
        f"{data['scored_above_threshold']}"
    )
    lines.append(
        f"  Apply pending + approved (ready to --submit): "
        f"{data['apply_pending_approved']}"
    )

    recent = data.get("recent_runs") or []
    if recent:
        lines.append("")
        lines.append("  Recent run logs:")
        for r in recent:
            err = " errors" if r.get("has_errors") else ""
            finished = r.get("finished_at") or "running?"
            lines.append(
                f"    #{r['id']} {r['stage']:16s} "
                f"jobs={r['jobs_processed']}  finished={finished}{err}"
            )

    lines.append("")
    lines.append(
        "  Next: discover/score/tailor/cover_letter via "
        "`python cli.py pipeline` (or cron); "
        "apply dry-run + `python cli.py review` are manual."
    )
    return "\n".join(lines)
