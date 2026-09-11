"""
Phase 7 — Application Automation (FR6)

Dry-run (default): for each job in status="cover_letter_ready", open the apply
URL, pick an ATS adapter, fill what we can, screenshot, set status="apply_pending".

Submit (explicit): only with --job-id for an *approved* apply_pending job
(Phase 8: `python cli.py review --approve --job-id <id>` first). Clicks final
Submit. Status becomes applied or failed.

Cascade (ADR-002):
  Tier 1 — deterministic adapters (LinkedIn / Workday / Greenhouse / confident
           generic) when the domain/form matches.
  Tier 2 — browser-use agent for tier2_ats domains and unrecognized forms.
  Tier 3 — manual_review when Tier 2 fails / CAPTCHA / low confidence.

Behavioral hard stops (aligned with ApplyPilot ideas, reimplemented):
  CAPTCHA / SSO / ID-verify / payment → manual_review
  blocked sites / marketplace / already applied → skipped
  expired job → failed (error prefix expired:)
  Never CapSolver; never invent credentials; never click Submit in dry-run.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from automation.agent_apply import (
    Tier2Budget,
    is_tier2_ats,
    run_tier2_apply,
    should_use_tier2,
)
from automation.browser import BrowserSession
from automation.form_detector import build_form_plan, load_ats_config
from automation.hard_stops import (
    detect_hard_stop,
    hard_stop_to_fill_status,
    is_url_blocked,
)
from automation.site_adapters import generic as generic_adapter
from automation.site_adapters import greenhouse as greenhouse_adapter
from automation.site_adapters import linkedin_easy_apply as linkedin_adapter
from automation.site_adapters import workday as workday_adapter
from automation.types import FillResult
from config.settings import get_settings
from core.review import is_application_approved
from core.tailor import load_profile
from db.models import Application, Job, RunLog
from db.session import get_db_session
from llm.client import LLMClient
from utils.logger import get_logger

logger = get_logger("jobapplypro.apply")


class ApplyError(RuntimeError):
    """Raised for stage-level abort conditions (e.g. submit without job_id)."""


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug or "job")[:max_len]


def select_adapter(url: str, html: str = ""):
    """Pick the most specific Tier 1 adapter that matches; generic is always last."""
    for adapter in (
        linkedin_adapter.adapter,
        workday_adapter.adapter,
        greenhouse_adapter.adapter,
        generic_adapter.adapter,
    ):
        if adapter.matches(url, html):
            return adapter
    return generic_adapter.adapter


def _materials_for_application(
    application: Application | None,
    profile: dict[str, Any],
) -> dict[str, Any]:
    materials: dict[str, Any] = {
        "resume_path": None,
        "cover_letter_path": None,
        "cover_letter_text": "",
        "tailored_resume_text": "",
        "job_url": None,
    }
    if application and application.tailored_resume_path:
        path = Path(application.tailored_resume_path)
        materials["resume_path"] = str(path) if path.exists() else None
        if path.exists() and path.suffix.lower() in {".md", ".txt", ".markdown"}:
            try:
                materials["tailored_resume_text"] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass
    if application and application.cover_letter_path:
        path = Path(application.cover_letter_path)
        materials["cover_letter_path"] = str(path) if path.exists() else None
        if path.exists() and path.suffix.lower() in {".md", ".txt", ".markdown"}:
            try:
                materials["cover_letter_text"] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass
    return materials


def _save_screenshot(
    page,
    job: Job,
    output_dir: Path,
    browser: BrowserSession | None = None,
) -> Path | None:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"job_{job.id}_{_slugify(job.company)}_{_slugify(job.title)}.png"
    path = output_dir / filename
    # Prefer the page we actually filled (external ATS tab), not the LinkedIn opener.
    shot_page = _best_screenshot_page(page, browser)
    try:
        if browser is not None and shot_page is browser.page:
            browser.screenshot(path, full_page=True)
        else:
            try:
                shot_page.wait_for_timeout(300)
            except Exception:
                pass
            shot_page.screenshot(path=str(path), full_page=True)
            logger.info("Screenshot saved to %s", path)
        return path
    except Exception as exc:
        logger.warning("Screenshot failed for job %s: %s", job.id, exc)
        return None


def _best_screenshot_page(page, browser: BrowserSession | None):
    """Prefer a non-LinkedIn tab when external apply opened a company ATS page."""
    try:
        context = page.context
        pages = list(context.pages)
    except Exception:
        return page
    for candidate in reversed(pages):
        try:
            url = (candidate.url or "").lower()
            if url and "linkedin.com" not in url and not url.startswith("about:"):
                return candidate
        except Exception:
            continue
    return page


def _result_from_hard_stop(stop, screenshot_path: str | None = None) -> FillResult:
    return FillResult(
        status=hard_stop_to_fill_status(stop),
        error=stop.error,
        screenshot_path=screenshot_path,
    )


def _run_tier2(
    *,
    job: Job,
    profile: dict[str, Any],
    materials: dict[str, Any],
    dry_run: bool,
    screenshot_dir: Path,
    tier2_budget: Tier2Budget | None,
    tier2_runner: Callable[..., Any] | None,
    ats_config: dict[str, Any],
) -> FillResult:
    """Shared Tier 2 entry — same safety wraps, cost guard, screenshot audit."""
    shot_target = (
        screenshot_dir
        / f"job_{job.id}_{_slugify(job.company)}_{_slugify(job.title)}_tier2.png"
    )
    logger.info(
        "Job %d routing to Tier 2 browser-use agent (%s)",
        job.id,
        job.url,
    )
    return run_tier2_apply(
        job_url=job.url,
        profile=profile,
        materials=materials,
        dry_run=dry_run,
        screenshot_path=shot_target,
        budget=tier2_budget,
        agent_runner=tier2_runner,
        ats_config=ats_config,
    )


def apply_to_job(
    job: Job,
    application: Application | None,
    profile: dict[str, Any],
    *,
    dry_run: bool = True,
    browser_factory: Callable[..., BrowserSession] | None = None,
    llm_client: LLMClient | None = None,
    screenshot_dir: Path | None = None,
    use_llm_detection: bool = False,
    tier2_budget: Tier2Budget | None = None,
    tier2_runner: Callable[..., Any] | None = None,
) -> FillResult:
    """
    Open the job URL, detect the form, fill via Tier 1 adapter or Tier 2 agent.
    When dry_run=True, never clicks final Submit.
    """
    settings = get_settings()
    screenshot_dir = Path(screenshot_dir or settings.screenshot_output_dir)
    factory = browser_factory or BrowserSession
    materials = _materials_for_application(application, profile)
    materials["job_url"] = job.url
    ats_config = load_ats_config()

    # --- Pre-browser policy checks (ApplyPilot queue filters) ---
    if is_url_blocked(job.url, ats_config):
        return FillResult(
            status="skipped",
            error="site_blocked: URL matched blocked sites/patterns",
        )

    # skip_tier2_ats=True (default): Lever/etc. are NOT hard-stopped — Tier 2 handles them.
    pre = detect_hard_stop(job.url, "", ats_config=ats_config)
    if pre is not None:
        return _result_from_hard_stop(pre)

    # Tailored resume is required for auto-apply (same idea as ApplyPilot claim gate).
    if not materials.get("resume_path"):
        return FillResult(
            status="manual_review",
            error="missing_materials: tailored resume path missing — run tailor stage first",
        )

    try:
        # Configured Tier 2 domains skip the Playwright Tier 1 session.
        if is_tier2_ats(job.url, ats_config):
            return _run_tier2(
                job=job,
                profile=profile,
                materials=materials,
                dry_run=dry_run,
                screenshot_dir=screenshot_dir,
                tier2_budget=tier2_budget,
                tier2_runner=tier2_runner,
                ats_config=ats_config,
            )

        route_tier2 = False
        with factory() as browser:
            browser.goto(job.url)

            # SSO redirect after navigation?
            current = browser.current_url
            stop = detect_hard_stop(current or job.url, "", ats_config=ats_config)
            if stop is not None:
                shot = _save_screenshot(browser.page, job, screenshot_dir, browser)
                return _result_from_hard_stop(
                    stop, str(shot) if shot else None
                )

            html = browser.content()

            if browser.detect_captcha(ats_config.get("captcha_signals")):
                shot = _save_screenshot(browser.page, job, screenshot_dir, browser)
                return FillResult(
                    status="manual_review",
                    error="captcha",
                    screenshot_path=str(shot) if shot else None,
                )

            stop = detect_hard_stop(current or job.url, html, ats_config=ats_config)
            if stop is not None:
                shot = _save_screenshot(browser.page, job, screenshot_dir, browser)
                return _result_from_hard_stop(
                    stop, str(shot) if shot else None
                )

            plan = build_form_plan(
                job.url,
                html,
                page=browser.page,
                llm_client=llm_client,
                use_llm=use_llm_detection,
            )

            # Hard Tier 3 exits that Tier 2 cannot help with.
            if plan.reason in {"captcha", "sso_required"}:
                shot = _save_screenshot(browser.page, job, screenshot_dir, browser)
                return FillResult(
                    status="manual_review",
                    error=plan.reason,
                    screenshot_path=str(shot) if shot else None,
                    adapter=plan.ats,
                )

            adapter = select_adapter(job.url, html)

            if should_use_tier2(
                job.url,
                plan_reason=plan.reason,
                plan_ats=plan.ats,
                plan_confidence=plan.confidence,
                adapter_name=adapter.name,
                form_min_confidence=settings.form_min_confidence,
                ats_config=ats_config,
            ):
                # Exit the Tier 1 browser context, then run the agent below.
                route_tier2 = True
            else:
                result = adapter.fill(
                    browser.page,
                    plan,
                    profile,
                    materials,
                    dry_run=dry_run,
                )

                # Post-interaction checks (CAPTCHA / SSO / expired after Apply click).
                post_url = browser.current_url or job.url
                post_html = ""
                try:
                    post_html = browser.content()
                except Exception:
                    pass

                if browser.detect_captcha(ats_config.get("captcha_signals")):
                    shot = _save_screenshot(browser.page, job, screenshot_dir, browser)
                    return FillResult(
                        status="manual_review",
                        error="captcha",
                        screenshot_path=str(shot) if shot else None,
                        fields_filled=result.fields_filled,
                        fields_skipped=result.fields_skipped,
                        adapter=adapter.name,
                    )

                stop = detect_hard_stop(post_url, post_html, ats_config=ats_config)
                if (
                    stop is not None
                    and stop.code == "already_applied"
                    and result.error
                    and "workday_account_required" in result.error
                ):
                    stop = None
                if stop is not None and stop.code in {
                    "sso_required",
                    "captcha",
                    "unsafe_verification",
                    "unsafe_payment",
                    "expired",
                    "already_applied",
                }:
                    shot = _save_screenshot(browser.page, job, screenshot_dir, browser)
                    out = _result_from_hard_stop(stop, str(shot) if shot else None)
                    out.fields_filled = result.fields_filled
                    out.fields_skipped = result.fields_skipped
                    out.adapter = adapter.name
                    return out

                shot = _save_screenshot(browser.page, job, screenshot_dir, browser)
                result.screenshot_path = str(shot) if shot else result.screenshot_path
                result.adapter = result.adapter or adapter.name
                return result

        if route_tier2:
            return _run_tier2(
                job=job,
                profile=profile,
                materials=materials,
                dry_run=dry_run,
                screenshot_dir=screenshot_dir,
                tier2_budget=tier2_budget,
                tier2_runner=tier2_runner,
                ats_config=ats_config,
            )
        return FillResult(status="failed", error="apply_to_job: unexpected empty path")
    except ApplyError:
        raise
    except Exception as exc:
        logger.error("apply_to_job failed for job %s: %s", job.id, exc, exc_info=True)
        return FillResult(status="failed", error=str(exc))


def _map_result_to_job_status(result: FillResult, *, submit: bool) -> tuple[str, str]:
    """
    Return (job.status, summary_bucket).
    Design statuses: apply_pending | applied | failed | manual_review | skipped
    """
    if result.status == "filled":
        return "apply_pending", "apply_pending"
    if result.status == "submitted":
        return "applied", "applied"
    if result.status == "manual_review":
        return "manual_review", "manual_review"
    if result.status == "skipped":
        return "skipped", "skipped"
    if result.status == "expired":
        # No dedicated Job.status; persist as failed with expired reason.
        return "failed", "failed"
    return "failed", "failed"


def run_apply_stage(
    *,
    dry_run: bool = True,
    submit: bool = False,
    job_id: int | None = None,
    session_scope: Callable | None = None,
    browser_factory: Callable[..., BrowserSession] | None = None,
    llm_client: LLMClient | None = None,
    profile_path: str | Path | None = None,
    screenshot_dir: str | Path | None = None,
    apply_fn: Callable[..., FillResult] | None = None,
    tier2_runner: Callable[..., Any] | None = None,
) -> dict[str, int]:
    """
    Orchestrate dry-run fills (or a single submit).

    dry_run=True (default): process cover_letter_ready → apply_pending
    submit=True: require job_id of an approved apply_pending job → applied/failed
    """
    if submit and dry_run:
        dry_run = False

    if submit and job_id is None:
        raise ApplyError(
            "Submit mode requires --job-id <id> of an approved apply_pending job"
        )

    settings = get_settings()
    session_scope = session_scope or get_db_session
    try:
        profile = load_profile(profile_path)
    except Exception as exc:
        raise ApplyError(str(exc)) from exc

    shot_dir = Path(screenshot_dir or settings.screenshot_output_dir)
    do_apply = apply_fn or apply_to_job
    tier2_budget = Tier2Budget(max_jobs=settings.tier2_max_jobs_per_run)

    summary = {
        "processed": 0,
        "apply_pending": 0,
        "applied": 0,
        "manual_review": 0,
        "failed": 0,
        "skipped": 0,
        "tier2_attempts": 0,
    }
    error_messages: list[str] = []

    with session_scope() as session:
        run_log = RunLog(
            stage="apply_submit" if submit else "apply_dry_run",
            jobs_processed=0,
        )
        session.add(run_log)
        session.flush()

        query = session.query(Job)
        if job_id is not None:
            query = query.filter(Job.id == job_id)
        elif submit:
            query = query.filter(Job.status == "apply_pending")
        else:
            query = query.filter(Job.status == "cover_letter_ready")

        eligible = query.order_by(Job.score.desc(), Job.id.asc()).all()
        if submit and job_id is not None and not eligible:
            raise ApplyError(f"No job with id={job_id}")

        logger.info(
            "Apply stage starting (dry_run=%s, submit=%s): %d job(s)",
            dry_run,
            submit,
            len(eligible),
        )

        for job in eligible:
            summary["processed"] += 1
            application = (
                session.query(Application)
                .filter(Application.job_id == job.id)
                .one_or_none()
            )

            if submit and job.status != "apply_pending":
                raise ApplyError(
                    f"job {job.id}: submit refused — status is {job.status!r}, "
                    f"expected 'apply_pending'"
                )

            if submit and not is_application_approved(application):
                raise ApplyError(
                    f"job {job.id}: submit refused — not approved. "
                    f"Run `python cli.py review --approve --job-id {job.id}` first"
                )

            if not submit and job.status != "cover_letter_ready" and job_id is None:
                summary["skipped"] += 1
                session.commit()
                continue

            budget_before = tier2_budget.used
            try:
                result: FillResult = do_apply(
                    job,
                    application,
                    profile,
                    dry_run=not submit,
                    browser_factory=browser_factory,
                    llm_client=llm_client,
                    screenshot_dir=shot_dir,
                    tier2_budget=tier2_budget,
                    tier2_runner=tier2_runner,
                )
            except TypeError as exc:
                # Custom apply_fn fixtures may not accept Tier 2 kwargs.
                msg = str(exc)
                if "unexpected keyword" not in msg and "got an unexpected" not in msg:
                    result = FillResult(status="failed", error=str(exc))
                    logger.error(
                        "Unexpected apply error for job %d: %s",
                        job.id,
                        exc,
                        exc_info=True,
                    )
                else:
                    try:
                        result = do_apply(
                            job,
                            application,
                            profile,
                            dry_run=not submit,
                            browser_factory=browser_factory,
                            llm_client=llm_client,
                            screenshot_dir=shot_dir,
                        )
                    except Exception as inner_exc:
                        result = FillResult(status="failed", error=str(inner_exc))
                        logger.error(
                            "Unexpected apply error for job %d: %s",
                            job.id,
                            inner_exc,
                            exc_info=True,
                        )
            except Exception as exc:
                result = FillResult(status="failed", error=str(exc))
                logger.error(
                    "Unexpected apply error for job %d: %s",
                    job.id,
                    exc,
                    exc_info=True,
                )

            if tier2_budget.used > budget_before:
                summary["tier2_attempts"] += tier2_budget.used - budget_before

            if application is None:
                application = Application(job_id=job.id)
                session.add(application)

            if result.screenshot_path:
                application.screenshot_path = result.screenshot_path

            if not submit and result.status == "filled":
                # A new dry-run screenshot must be re-reviewed before submit.
                if application.review_decision:
                    logger.info(
                        "Job %d re-filled; clearing review_decision=%s",
                        job.id,
                        application.review_decision,
                    )
                application.review_decision = None
                application.reviewed_at = None

            note = (
                f"Apply({'submit' if submit else 'dry-run'}) "
                f"adapter={result.adapter} status={result.status}"
            )
            if result.error:
                note += f" | {result.error}"
            if result.fields_filled:
                note += f" | filled={result.fields_filled}"
            if result.fields_skipped:
                note += f" | skipped_fields={result.fields_skipped}"
            job.reasoning = f"{job.reasoning}\n{note}" if job.reasoning else note

            new_status, bucket = _map_result_to_job_status(result, submit=submit)
            job.status = new_status
            summary[bucket] = summary.get(bucket, 0) + 1

            if result.status == "submitted":
                application.submitted_at = datetime.now(timezone.utc)
                logger.info("Submitted application for job %d", job.id)
            elif submit and result.status == "filled":
                logger.warning(
                    "Submit requested for job %d but adapter did not click Submit "
                    "(status=filled) — job stays apply_pending",
                    job.id,
                )
            elif result.status == "filled":
                logger.info(
                    "Dry-run filled job %d (%s) → apply_pending (%s)",
                    job.id,
                    job.title,
                    result.screenshot_path,
                )
            elif result.status == "manual_review":
                logger.warning(
                    "Job %d routed to manual_review: %s", job.id, result.error
                )
                error_messages.append(f"job {job.id}: {result.error}")
            elif result.status == "skipped":
                logger.info("Job %d skipped: %s", job.id, result.error)
            elif result.status == "expired":
                logger.warning("Job %d expired: %s", job.id, result.error)
                error_messages.append(f"job {job.id}: {result.error}")
            else:
                logger.error("Job %d apply failed: %s", job.id, result.error)
                error_messages.append(f"job {job.id}: {result.error}")

            session.commit()

        run_log.jobs_processed = summary["processed"]
        run_log.finished_at = datetime.now(timezone.utc)
        run_log.errors = "\n".join(error_messages) if error_messages else None
        session.commit()

    if summary["processed"] == 0:
        logger.info(
            "No eligible jobs for apply stage (dry_run=%s, submit=%s, job_id=%s)",
            dry_run,
            submit,
            job_id,
        )

    logger.info("Apply stage complete: %s", summary)
    return summary
