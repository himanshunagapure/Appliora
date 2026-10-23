"""
Phase 7 — Application Automation (FR6)

Dry-run (default): for each job in status="cover_letter_ready", open the apply
URL, pick an ATS adapter, fill what we can, screenshot, set status="apply_pending".
Does not click Submit.

Unattended submit (AUTO_SUBMIT=true / auto=True): cover_letter_ready and
apply_pending jobs are filled and Submit is clicked. No per-job approval.
CAPTCHA, OTP, payment, and ID checks still stop as manual_review.

One-job submit (explicit): `--submit --job-id` still requires an approved
apply_pending job.

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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

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
from automation.site_login import try_credential_login
from automation.site_adapters import generic as generic_adapter
from automation.site_adapters import greenhouse as greenhouse_adapter
from automation.site_adapters import linkedin_easy_apply as linkedin_adapter
from automation.site_adapters import workday as workday_adapter
from automation.types import FillResult
from config.settings import get_settings
from core.experience import experience_gate
from core.review import is_application_approved
from core.tailor import load_profile
from db.models import Application, Job, RunLog
from db.session import get_db_session
from llm.client import LLMClient
from utils.logger import get_logger

logger = get_logger("jobapplypro.apply")

# Job aggregators that rarely host the real ATS form. Click Apply first so we
# can fill Greenhouse/Workday/LinkedIn with Tier 1 instead of dumping to Tier 2.
_JOB_BOARD_HOSTS = (
    "indeed.com",
    "naukri.com",
    "glassdoor.com",
    "simplyhired.com",
)

_JOB_BOARD_APPLY_SELECTORS = [
    "button#indeedApplyButton",
    "[data-testid='indeedApplyButton']",
    "button[aria-label*='Apply now' i]",
    "a[aria-label*='Apply now' i]",
    "button:has-text('Apply on company site')",
    "a:has-text('Apply on company site')",
    "button:has-text('Apply now')",
    "a:has-text('Apply now')",
    "button:has-text('Easy Apply')",
    "a:has-text('Easy Apply')",
    "[data-testid*='apply' i]",
]

# False manual_review from the last apply bugs — reset so dry-run can retry.
_RETRYABLE_MANUAL_REVIEW_MARKERS = (
    "tier2_budget_exhausted",
    "BrowserStartEvent",
    "timed out after 30",
    "Event handler BrowserStartEvent",
)


class ApplyError(RuntimeError):
    """Raised for stage-level abort conditions (e.g. submit without job_id)."""


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug or "job")[:max_len]


def browser_state_path_for_job(job_id: int) -> Path:
    """Per-job Playwright storage_state path (cookies/localStorage for submit continuity)."""
    settings = get_settings()
    base = Path(getattr(settings, "browser_state_dir", None) or "output/browser_state")
    return base / f"job_{job_id}.json"


def is_job_board_url(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return any(host == h or host.endswith(f".{h}") for h in _JOB_BOARD_HOSTS)


def is_retryable_manual_review(text: str | None) -> bool:
    """Budget lock / Chrome-start timeouts are operational, not 'needs a human'.

    Only the latest Apply(...) note counts — older BrowserStart failures must
    not keep re-queuing a job that later hit captcha or a real login wall.
    """
    blob = text or ""
    apply_notes = [ln for ln in blob.splitlines() if "Apply(" in ln]
    last = apply_notes[-1] if apply_notes else blob
    lowered = last.lower()
    if any(
        token in lowered
        for token in ("captcha", "sso_required", "login_timeout", "missing_materials")
    ) and "tier2_budget_exhausted" not in last:
        return False
    return any(marker in last for marker in _RETRYABLE_MANUAL_REVIEW_MARKERS)


def recover_retryable_manual_reviews(session) -> int:
    """Put wrongly queued jobs back to cover_letter_ready for the next dry-run."""
    jobs = session.query(Job).filter(Job.status == "manual_review").all()
    recovered = 0
    for job in jobs:
        if not is_retryable_manual_review(job.reasoning):
            continue
        job.status = "cover_letter_ready"
        note = "recovered from retryable manual_review (budget/browser-start)"
        job.reasoning = f"{job.reasoning}\n{note}" if job.reasoning else note
        recovered += 1
    if recovered:
        logger.info(
            "Recovered %d job(s) from manual_review (budget/browser-start timeouts)",
            recovered,
        )
    return recovered


def follow_job_board_apply(browser) -> bool:
    """
    Click Apply on Indeed/Naukri/etc. and stay on whatever ATS tab opens.

    Returns True when the URL changed (company site / apply form).
    """
    page = getattr(browser, "page", None)
    if page is None or not hasattr(page, "locator"):
        return False
    try:
        start = (getattr(page, "url", None) or getattr(browser, "current_url", "") or "").rstrip("/")
    except Exception:
        start = ""

    for selector in _JOB_BOARD_APPLY_SELECTORS:
        loc = page.locator(selector).first
        try:
            if loc.count() == 0:
                continue
            if not loc.is_visible(timeout=1200):
                continue
        except Exception:
            continue

        popup = None
        try:
            try:
                with page.expect_popup(timeout=5000) as pop_info:
                    loc.click(timeout=5000)
                popup = pop_info.value
            except Exception:
                loc.click(timeout=5000)
        except Exception as exc:
            logger.debug("Job-board Apply selector %s failed: %s", selector, exc)
            continue

        try:
            if popup is not None:
                popup.wait_for_load_state("domcontentloaded", timeout=20000)
                if hasattr(browser, "adopt_page"):
                    browser.adopt_page(popup)
                else:
                    browser._page = popup
            else:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
                wait = getattr(browser, "wait_until_settled", None)
                if callable(wait):
                    wait(timeout_ms=8000)
        except Exception as exc:
            logger.debug("After Apply click wait failed: %s", exc)

        try:
            new_url = (browser.page.url if hasattr(browser, "page") else "") or ""
        except Exception:
            new_url = getattr(browser, "current_url", "") or ""
        if new_url.rstrip("/") and new_url.rstrip("/") != start:
            logger.info("Job-board Apply navigated %s → %s", start, new_url)
            return True

    return False


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
        if not path.exists():
            # Prefer sibling DOCX/PDF/MD if the stored path was deleted or is stale.
            for alt in (
                path.with_suffix(".docx"),
                path.with_suffix(".pdf"),
                path.with_suffix(".md"),
                path.with_suffix(".txt"),
            ):
                if alt.exists() and alt != path:
                    path = alt
                    break
        materials["resume_path"] = str(path) if path.exists() else None
        if path.exists():
            try:
                from automation.resume_upload import read_resume_text

                materials["tailored_resume_text"] = read_resume_text(path)
            except Exception:
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
        state_path = browser_state_path_for_job(job.id)
        # Reuse dry-run cookies/localStorage on submit (and on re-dry-run).
        factory_kwargs: dict[str, Any] = {}
        if state_path.is_file():
            factory_kwargs["storage_state"] = state_path
            logger.info(
                "Job %d loading prior browser storage_state from %s",
                job.id,
                state_path,
            )

        with factory(**factory_kwargs) as browser:
            browser.goto(job.url)

            login = try_credential_login(
                browser.page,
                return_url=job.url,
                settings=settings,
            )
            if login.checkpoint or login.captcha:
                shot = _save_screenshot(browser.page, job, screenshot_dir, browser)
                return FillResult(
                    status="manual_review",
                    error=(
                        "captcha"
                        if login.captcha
                        else "login_required: verification challenge after sign-in"
                    ),
                    screenshot_path=str(shot) if shot else None,
                )

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
            listing_url = current or job.url

            # Indeed/Naukri listings are not the ATS. Click Apply in this same
            # Playwright session so Greenhouse/Workday can stay on Tier 1.
            if is_job_board_url(listing_url) and (
                should_use_tier2(
                    listing_url,
                    plan_reason=plan.reason,
                    plan_ats=plan.ats,
                    plan_confidence=plan.confidence,
                    adapter_name=adapter.name,
                    form_min_confidence=settings.form_min_confidence,
                    ats_config=ats_config,
                )
                or plan.apply_button
            ):
                logger.info(
                    "Job %d is a job-board listing (%s) — clicking Apply before Tier 2",
                    job.id,
                    listing_url,
                )
                if follow_job_board_apply(browser):
                    current = browser.current_url or listing_url
                    html = browser.content()
                    if browser.detect_captcha(ats_config.get("captcha_signals")):
                        shot = _save_screenshot(
                            browser.page, job, screenshot_dir, browser
                        )
                        return FillResult(
                            status="manual_review",
                            error="captcha",
                            screenshot_path=str(shot) if shot else None,
                        )
                    stop = detect_hard_stop(current, html, ats_config=ats_config)
                    if stop is not None:
                        shot = _save_screenshot(
                            browser.page, job, screenshot_dir, browser
                        )
                        return _result_from_hard_stop(
                            stop, str(shot) if shot else None
                        )
                    plan = build_form_plan(
                        current,
                        html,
                        page=browser.page,
                        llm_client=llm_client,
                        use_llm=use_llm_detection,
                    )
                    if plan.reason in {"captcha", "sso_required"}:
                        shot = _save_screenshot(
                            browser.page, job, screenshot_dir, browser
                        )
                        return FillResult(
                            status="manual_review",
                            error=plan.reason,
                            screenshot_path=str(shot) if shot else None,
                            adapter=plan.ats,
                        )
                    adapter = select_adapter(current, html)
                    logger.info(
                        "Job %d after Apply click: url=%s adapter=%s confidence=%.2f",
                        job.id,
                        current,
                        adapter.name,
                        plan.confidence,
                    )

            if should_use_tier2(
                current or job.url,
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
                # Persist cookies/localStorage so submit (new process) is not cold.
                if result.status in {"filled", "submitted"}:
                    try:
                        browser.save_storage_state(state_path)
                    except Exception as exc:
                        logger.warning(
                            "Could not save storage_state for job %d: %s",
                            job.id,
                            exc,
                        )
                return result

        if route_tier2:
            # Persistent Playwright Chrome holds SingletonLock; wait before browser-use.
            if getattr(settings, "browser_user_data_dir", None):
                time.sleep(1.5)
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
    if result.status == "deferred":
        # Stay eligible for the next apply dry-run (do not burn as manual_review).
        return "cover_letter_ready", "deferred"
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
    auto: bool = False,
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
    Fill applications and, when submit/auto is set, click Submit.

    dry_run=True: cover_letter_ready → apply_pending (no Submit click).
    submit=True with a job_id: one approved apply_pending job → applied.
    auto=True: every cover_letter_ready and apply_pending job is filled and
    submitted. No per-job approval. CAPTCHA / OTP / payment still stop.
    """
    if auto:
        submit = True
        dry_run = False
    elif submit and dry_run:
        dry_run = False

    if submit and not auto and job_id is None:
        raise ApplyError(
            "Submit mode requires --job-id <id> of an approved apply_pending job. "
            "For unattended submit set AUTO_SUBMIT=true (pipeline) or pass auto=True."
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
        "deferred": 0,
        "recovered": 0,
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

        if not submit:
            summary["recovered"] = recover_retryable_manual_reviews(session)
            session.flush()

        query = session.query(Job)
        if job_id is not None:
            query = query.filter(Job.id == job_id)
        elif auto:
            query = query.filter(Job.status.in_(("cover_letter_ready", "apply_pending")))
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

            if submit and not auto and job.status != "apply_pending":
                raise ApplyError(
                    f"job {job.id}: submit refused — status is {job.status!r}, "
                    f"expected 'apply_pending'"
                )

            if submit and not auto and not is_application_approved(application):
                raise ApplyError(
                    f"job {job.id}: submit refused — not approved. "
                    f"Run `python cli.py review --approve --job-id {job.id}` first"
                )

            if (
                not submit
                and not auto
                and job.status != "cover_letter_ready"
                and job_id is None
            ):
                summary["skipped"] += 1
                session.commit()
                continue

            qualified, yoe_reason = experience_gate(
                profile, job.title or "", job.description or ""
            )
            if not qualified:
                logger.info("Job %d skipped (years of experience): %s", job.id, yoe_reason)
                job.status = "skipped"
                job.reasoning = (
                    f"{job.reasoning}\n{yoe_reason}" if job.reasoning else yoe_reason
                )
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
                # Keep prior approval across re-dry-run — screenshot refresh alone
                # must not force a second review. Operator should still open the
                # new screenshot before --submit.
                if application.review_decision == "approved":
                    logger.warning(
                        "Job %d re-filled after approval — keeping review_decision=approved; "
                        "re-open the new screenshot before --submit",
                        job.id,
                    )
                    note_extra = (
                        " | approval kept after re-dry-run; re-check screenshot"
                    )
                else:
                    note_extra = ""
            else:
                note_extra = ""

            note = (
                f"Apply({'submit' if submit else 'dry-run'}) "
                f"adapter={result.adapter} status={result.status}"
            )
            if note_extra:
                note += note_extra
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
            elif result.status == "deferred":
                logger.info(
                    "Job %d deferred to next apply run: %s", job.id, result.error
                )
            elif result.status == "skipped":
                logger.info("Job %d skipped: %s", job.id, result.error)
            elif result.status == "expired":
                logger.warning("Job %d expired: %s", job.id, result.error)
                error_messages.append(f"job {job.id}: {result.error}")
            else:
                err = result.error or ""
                if "Async API" in err or "Sync API inside the asyncio" in err:
                    # browser-use leaves an event loop; the next Playwright call
                    # in this process is unusable. Keep the job queued.
                    job.status = "cover_letter_ready"
                    logger.warning(
                        "Job %d browser loop clash — left cover_letter_ready: %s",
                        job.id,
                        err.splitlines()[0][:160],
                    )
                    summary["failed"] -= 1
                    summary["deferred"] = summary.get("deferred", 0) + 1
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
