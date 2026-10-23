"""
Phase 9 — Pipeline orchestration.

Runs stages in sequence. Each stage already commits per job and isolates
per-job failures; this module adds a run-level guard so systemic errors
(DB down, missing profile, etc.) abort cleanly without a messy traceback
as the only signal.

Default scheduled path:
  discover → score → tailor → cover_letter → apply

When AUTO_SUBMIT=true (default), apply fills the form and clicks Submit.
CAPTCHA, OTP, payment, and ID checks still land in manual_review.
Set AUTO_SUBMIT=false to fill without clicking Submit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError

from utils.logger import get_logger

logger = get_logger("jobapplypro.pipeline")

# Materials stages. Apply is appended when the pipeline should also submit.
DEFAULT_STAGES: tuple[str, ...] = ("discover", "score", "tailor", "cover_letter")
ALL_AUTO_STAGES: tuple[str, ...] = (*DEFAULT_STAGES, "apply")

# Connection / DB failures abort the run. FileNotFoundError is an OSError
# subclass and must not be here — it is a stage-level error and should honor
# --continue-on-error (missing profile is already ScoringError/TailoringError).
SYSTEMIC_EXCEPTIONS = (
    OperationalError,
    DBAPIError,
    SQLAlchemyError,
    ConnectionError,
    TimeoutError,
    BrokenPipeError,
)


class PipelineError(RuntimeError):
    """Raised when the pipeline aborts on a systemic / stage-level failure."""


@dataclass
class StageResult:
    name: str
    ok: bool
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class PipelineResult:
    stages: list[StageResult] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str | None = None

    @property
    def ok(self) -> bool:
        return not self.aborted and all(s.ok for s in self.stages)


def check_database(*, session_scope: Callable | None = None) -> None:
    """
    Fail fast if the DB is unreachable, then ensure Alembic migrations are applied.
    Raises PipelineError with a clear message (global run guard).
    """
    from sqlalchemy import text

    from db.migrate import ensure_schema
    from db.session import get_db_session

    scope = session_scope or get_db_session
    try:
        with scope() as session:
            session.execute(text("SELECT 1"))
    except (*SYSTEMIC_EXCEPTIONS, OSError) as exc:
        from utils.db_url import pg_socket_ready, sandbox_localhost_hint

        extra = ""
        if "connection refused" in str(exc).lower() and not pg_socket_ready():
            extra = f" {sandbox_localhost_hint()}"
        raise PipelineError(
            f"Database unavailable — aborting before stages run: {exc}{extra}"
        ) from exc
    except Exception as exc:
        # Unknown connection failures still abort cleanly.
        msg = str(exc).lower()
        if any(
            tok in msg
            for tok in (
                "could not connect",
                "connection refused",   
                "connection reset",
                "server closed",
                "operationalerror",
                "password authentication failed",
                "no such table",
                "does not exist",
            )
        ):
            raise PipelineError(
                f"Database unavailable — aborting before stages run: {exc}"
            ) from exc
        raise

    # Connectivity OK — bring schema to head (Phase 8 review columns, etc.).
    # Skip when a test injects a custom session_scope (in-memory fixtures).
    if session_scope is None:
        try:
            ensure_schema()
        except Exception as exc:
            raise PipelineError(
                f"Database schema upgrade failed — run `alembic upgrade head` "
                f"manually if this persists: {exc}"
            ) from exc


def _run_discover() -> dict[str, Any]:
    from core.discover import run_discovery

    result = run_discovery()
    return result if isinstance(result, dict) else {"ok": True}


def _run_score() -> dict[str, Any]:
    from core.score import run_scoring_stage

    return run_scoring_stage()


def _run_tailor() -> dict[str, Any]:
    from core.tailor import run_tailoring_stage

    return run_tailoring_stage()


def _run_cover_letter() -> dict[str, Any]:
    from core.cover_letter import run_cover_letter_stage

    return run_cover_letter_stage()


def _run_apply() -> dict[str, Any]:
    from config.settings import get_settings
    from core.apply import run_apply_stage

    if get_settings().auto_submit:
        # Fill and click Submit. No per-job human approval.
        return run_apply_stage(dry_run=False, submit=True, auto=True)
    return run_apply_stage(dry_run=True, submit=False)


STAGE_RUNNERS: dict[str, Callable[..., dict[str, Any]]] = {
    "discover": _run_discover,
    "score": _run_score,
    "tailor": _run_tailor,
    "cover_letter": _run_cover_letter,
    "apply": _run_apply,
}


def run_pipeline(
    stages: Sequence[str] | None = None,
    *,
    include_apply: bool = False,
    stop_on_error: bool = True,
    check_db: bool = True,
    session_scope: Callable | None = None,
    stage_runners: dict[str, Callable[..., dict[str, Any]]] | None = None,
) -> PipelineResult:
    """
    Run pipeline stages in order.

    - Per-job isolation lives inside each stage (commit per job).
    - This function catches stage-level / systemic failures and either
      stops (default) or records the failure and continues.
    """
    if stages is None:
        from config.settings import get_settings

        want_apply = include_apply or get_settings().auto_submit
        stages = (*DEFAULT_STAGES, "apply") if want_apply else DEFAULT_STAGES
    else:
        stages = tuple(s.strip().lower() for s in stages if str(s).strip())
        if not stages:
            err = "No stages to run"
            logger.error(err)
            result = PipelineResult(aborted=True, abort_reason=err)
            return result

    runners = stage_runners or STAGE_RUNNERS
    result = PipelineResult()

    logger.info("Pipeline starting: stages=%s", list(stages))

    if check_db:
        try:
            check_database(session_scope=session_scope)
        except PipelineError as exc:
            logger.error("%s", exc)
            result.aborted = True
            result.abort_reason = str(exc)
            return result

    for name in stages:
        name = name.strip().lower()
        if name not in runners:
            err = f"Unknown stage {name!r}"
            logger.error(err)
            result.stages.append(StageResult(name=name, ok=False, error=err))
            result.aborted = True
            result.abort_reason = err
            break

        logger.info("Pipeline stage begin: %s", name)
        try:
            summary = runners[name]()
            if not isinstance(summary, dict):
                summary = {"result": summary}
            result.stages.append(StageResult(name=name, ok=True, summary=summary))
            logger.info("Pipeline stage complete: %s → %s", name, summary)
        except PipelineError as exc:
            logger.error("Pipeline aborted during %s: %s", name, exc)
            result.stages.append(StageResult(name=name, ok=False, error=str(exc)))
            result.aborted = True
            result.abort_reason = str(exc)
            break
        except SYSTEMIC_EXCEPTIONS as exc:
            msg = f"Systemic failure in stage {name}: {exc}"
            logger.error(msg, exc_info=True)
            result.stages.append(StageResult(name=name, ok=False, error=msg))
            result.aborted = True
            result.abort_reason = msg
            break
        except Exception as exc:
            # Stage-level abort (e.g. ScoringError on missing profile).
            msg = f"Stage {name} aborted: {exc}"
            logger.error(msg, exc_info=True)
            result.stages.append(StageResult(name=name, ok=False, error=msg))
            if stop_on_error:
                result.aborted = True
                result.abort_reason = msg
                break

    if result.aborted:
        logger.error("Pipeline aborted: %s", result.abort_reason)
    elif not result.ok:
        failed = [s.name for s in result.stages if not s.ok]
        logger.warning(
            "Pipeline finished with stage failures: %s",
            ", ".join(failed) or "(unknown)",
        )
    else:
        logger.info("Pipeline finished successfully (%d stage(s))", len(result.stages))
    return result


def format_pipeline_report(result: PipelineResult) -> str:
    lines = ["Pipeline report"]
    for stage in result.stages:
        mark = "ok" if stage.ok else "FAIL"
        lines.append(f"  [{mark}] {stage.name}")
        if stage.summary:
            # Compact one-line summary of int-ish counters.
            bits = []
            for k, v in stage.summary.items():
                if isinstance(v, (int, float, str, bool)):
                    bits.append(f"{k}={v}")
            if bits:
                lines.append("         " + "  ".join(bits))
        if stage.error:
            lines.append(f"         error: {stage.error}")
    if result.aborted:
        lines.append(f"Aborted: {result.abort_reason}")
    elif not result.ok:
        failed = [s.name for s in result.stages if not s.ok]
        lines.append(f"Status: complete with failures ({', '.join(failed)})")
    else:
        lines.append("Status: complete")
    return "\n".join(lines)
