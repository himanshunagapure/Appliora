"""
Tier 2 generalized apply agent (ADR-002).

Wraps a browser-use Agent for domains that have no Tier 1 deterministic
adapter (or whose form detection confidence is too low). Shares the same
safety model as Tier 1: CAPTCHA = detect only, dry-run never clicks final
Submit, hard stops / blocked SSO still win, jobs land in apply_pending for
human review.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from automation.form_detector import is_manual_ats, load_ats_config, profile_value_map
from automation.hard_stops import detect_hard_stop, hard_stop_to_fill_status
from automation.types import FillResult
from config.settings import get_settings
from utils.logger import get_logger

logger = get_logger("jobapplypro.agent_apply")

ADAPTER_NAME = "browser_use_tier2"

# ATS families with no Tier 1 adapter — always use the agent, even on a
# company career host that only fingerprints as Lever/Ashby in HTML.
_TIER2_ATS_FAMILIES = frozenset({"lever", "ashby", "workable"})

_CAPTCHA_MARKERS = (
    "g-recaptcha",
    "h-captcha",
    "hcaptcha",
    "turnstile",
    "recaptcha",
    "funcaptcha",
    "arkoselabs",
)

_SSO_MARKERS = (
    "sso required",
    "sso wall",
    "blocked_sso",
    "okta login",
    "microsoft login",
    "indeed login",
    "login/signup",
    "sign in to indeed",
)

_FAILURE_MARKERS = (
    "could not fill",
    "unable to fill",
    "cannot fill",
    "login required",
    "manual review",
)

_SUCCESS_MARKERS = (
    "ready for review",
    "ready for human",
    "stopped before submit",
    "form complete",
    "application ready",
)

# Narratives that mean the site accepted the application, even when the agent
# forgets the literal "outcome=submitted" token.
_SUBMIT_CONFIRMATIONS = (
    "successfully submitted",
    "has been submitted",
    "application was submitted",
    "submission confirmed",
    "application was confirmed",
    "we have received your application",
    "thank you for applying",
    "protocolo",
    "confirmation number",
)

_SUBMIT_NEGATIONS = (
    "did not submit",
    "didn't submit",
    "was not submitted",
    "not been submitted",
    "not submitted",
    "stopped before submit",
    "before submission",
    "without submitting",
    "final submit was not clicked",
)


@dataclass
class Tier2Budget:
    """Per-run token/cost guard for Tier 2 agent attempts."""

    max_jobs: int
    used: int = 0

    def remaining(self) -> int:
        return max(0, self.max_jobs - self.used)

    def try_consume(self) -> bool:
        if self.used >= self.max_jobs:
            return False
        self.used += 1
        return True


def is_tier2_ats(url: str, ats_config: dict[str, Any] | None = None) -> bool:
    """True when the URL host is listed under tier2_ats (or legacy manual_ats)."""
    return is_manual_ats(url, ats_config)


def build_agent_task(
    *,
    job_url: str,
    profile: dict[str, Any],
    materials: dict[str, Any],
    dry_run: bool = True,
) -> str:
    """Natural-language task for browser-use, with hard safety constraints."""
    values = profile_value_map(profile)
    resume_path = materials.get("resume_path") or ""
    cover_path = materials.get("cover_letter_path") or ""
    cover_text = (materials.get("cover_letter_text") or "")[:2500]
    tailored = (materials.get("tailored_resume_text") or "")[:3000]

    city = values.get("city") or ""
    location = values.get("location") or ""
    # Autocomplete widgets often need a short query, not the full address line.
    location_hint = city or (location.split(",")[0].strip() if location else "")

    submit_rule = (
        "CRITICAL: This is a DRY RUN. Fill every VISIBLE required field you can, "
        "upload the resume if a file input exists, but NEVER click the final "
        "Submit / Apply / Send application button. When the form looks complete "
        "(or only optional unanswered fields remain), STOP and report "
        "outcome=filled."
        if dry_run
        else (
            "Submit the application. After required fields are filled and the "
            "resume is uploaded, click the final Submit / Submit application / "
            "Send application button. Do not stop on the review page. "
            "Report outcome=submitted only after the site confirms the application "
            "was sent."
        )
    )

    if dry_run:
        done_criteria = """
Done criteria (outcome=filled). This is a DRY RUN:
  - You are on the application FORM (not a job-description Overview page)
  - Resume uploaded (if a file input exists)
  - Name, email, and phone filled when those fields exist
  - Location filled OR skipped after two failed attempts
  - Visible required questions answered when profile has answers
  - Final Submit was NOT clicked
  - Scrolled to top for the audit screenshot
  Then stop immediately with outcome=filled even if some optional fields remain.
""".strip()
    else:
        done_criteria = """
Done criteria (outcome=submitted). This is a REAL SUBMIT, not a dry run:
  - Fill the form as above
  - Click the final Submit / Submit application / Send application button
  - Do NOT stop on the Review page
  - Do NOT say you stopped because of dry-run instructions
  - Report outcome=submitted only after the site confirms the application was sent
""".strip()

    return f"""
You are filling a job application form in a real browser.

Job URL (navigate here if needed): {job_url}

Applicant profile values (use exactly; do not invent facts):
{json.dumps(values, indent=2)}

Location autocomplete hint (prefer this short string for city/location widgets):
  {location_hint or "(none)"}
Full location string (use only if the field is free-text, not autocomplete):
  {location or "(none)"}

Resume file path (upload this to any resume/CV file input): {resume_path or "(none)"}
Cover letter file path (if a file upload is offered): {cover_path or "(none)"}
Cover letter text (paste into a cover-letter textarea if present):
---
{cover_text or "(none)"}
---
Tailored resume text (reference only; prefer uploading the resume file):
---
{tailored or "(none)"}
---

Priority order (do these first, in as few steps as possible):
  A) If the page is a job description (Overview) with an Apply button or an
     Application tab, click Apply / open the Application tab first — do not stop
     on the overview page
  B) Dismiss cookie/privacy banners
  C) Upload resume to the file input
  D) Full name, Email, Phone
  E) Current location / city (see autocomplete rules below)
  F) Visible required yes/no or select questions (work auth, sponsorship, etc.)
  G) Other visible required fields only
  H) Optional EEO: if prefer-not-to-say / decline options are provided above, use them;
     when Name and Date fields appear under Disability, fill them from the profile

Rules:
1. {submit_rule}
2. Never solve CAPTCHAs. If you see a CAPTCHA / bot wall / Cloudflare challenge,
   stop immediately and report outcome=captcha.
3. Never invent credentials, never create accounts with fake emails/passwords,
   never enter SSN / payment / bank details, never complete ID selfie verification.
4. If the page requires SSO / Google / Microsoft / Okta login you cannot complete,
   stop and report outcome=sso_required.
5. ONLY fill fields that are actually visible on the page. The profile JSON may
   list keys that are NOT on this form (Over 18, How did you hear, Address, Postal
   Code, Previously worked here, etc.). If a key is not on the page, SKIP it —
   do NOT scroll endlessly hunting for missing profile keys.
6. At most TWO attempts per field. If a field still fails, leave it and move on.
7. Location / city autocomplete (Lever, Greenhouse, Workable, etc.):
   - Click the location / city / "Current location" field and clear any placeholder
   - Type ONLY the short city hint above (e.g. "{location_hint or "city"}"),
     one short burst — do NOT paste the full address or country
   - Wait ~1–2 seconds for the suggestion dropdown to appear
   - Click the best matching suggestion (prefer city + region if listed)
   - If no dropdown: press ArrowDown then Enter once, or Tab once
   - If still empty after TWO attempts total, leave location blank and continue —
     never loop on location; never type country-only or full street address into
     an autocomplete widget
8. Batch multiple fill/click actions in one step when possible (name+email+phone).
9. Prefer truthful answers from the profile. If a required question cannot be
   answered from the profile, leave it blank and note it — do not fabricate.
10. Do not open extra tabs. Stay on the application form.
11. Before finishing, scroll to the TOP of the application form so the final
    browser screenshot shows the filled name/email/phone/resume area (audit).

{done_criteria}

When finished, your final answer MUST include one of:
  outcome=filled — form filled, stopped before submit (dry-run only)
  outcome=submitted — you clicked Submit and the site accepted it
  outcome=captcha — CAPTCHA / bot wall blocked progress
  outcome=sso_required — login wall you cannot pass
  outcome=low_confidence — form too ambiguous or incomplete to trust
  outcome=failed — other hard failure (brief reason)
""".strip()


def prepare_tier2_materials(materials: dict[str, Any]) -> dict[str, Any]:
    """Copy materials and convert markdown resumes to DOCX for ATS file inputs."""
    out = dict(materials)
    resume = out.get("resume_path")
    if not resume:
        return out
    try:
        from automation.resume_upload import ensure_uploadable_resume

        converted = ensure_uploadable_resume(resume)
        out["resume_path"] = str(converted)
        if converted != Path(resume):
            logger.info("Tier 2 resume prepared for upload: %s", converted)
    except Exception as exc:
        logger.warning("Tier 2 resume conversion skipped (%s); using %s", exc, resume)
    return out


def _core_fields_filled(text: str) -> bool:
    """Heuristic: agent narrative shows the main contact fields were filled."""
    lower = (text or "").lower()
    acted = any(
        tok in lower
        for tok in ("filled", "entered", "typed", "uploaded", "upload", "attached")
    )
    if not acted:
        return False
    has_name = any(
        tok in lower
        for tok in (
            "full name",
            "first name",
            "legal name",
            "filled the 'full name'",
            "name field",
        )
    )
    has_email = "email" in lower and ("@" in text or "filled the 'email'" in lower or "email'" in lower)
    has_phone = "phone" in lower
    has_resume = "resume" in lower and (
        "upload" in lower or "uploaded" in lower or "attach" in lower
    )
    stopped = any(
        tok in lower
        for tok in (
            "stopped before submit",
            "before submission",
            "did not submit",
            "not clicked submit",
            "never click",
            "dry run",
        )
    )
    # Require contact trio; resume upload is strong signal when mentioned.
    if (has_name and has_email and has_phone) or (
        has_resume and has_name and has_email
    ) or (has_resume and has_name and has_phone and stopped):
        return True
    # Agent summaries like "form has been filled … Email … Phone … resume uploaded"
    if acted and has_email and has_phone and has_resume:
        return True
    return False

def _looks_submitted(text: str) -> bool:
    """True when the agent describes a confirmed submit, not a dry-run fill."""
    lower = text.lower()
    if any(tok in lower for tok in _SUBMIT_NEGATIONS):
        return False
    return any(tok in lower for tok in _SUBMIT_CONFIRMATIONS)


def _parse_outcome(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(
        r"outcome\s*=\s*(filled|submitted|captcha|sso_required|low_confidence|failed|expired)",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).lower()
    if _looks_submitted(text):
        return "submitted"
    lower = text.lower()
    # Prefer success phrasing over incidental words like "log in" / "failed".
    if any(tok in lower for tok in _SUCCESS_MARKERS):
        return "filled"
    # Agent often says "stopped before submission" without outcome=filled.
    if _core_fields_filled(text) and any(
        tok in lower
        for tok in (
            "stopped before submit",
            "before submission",
            "partially filled",
            "as requested",
            "did not submit",
            "form has been filled",
            "application form has been filled",
            "resume file",
            "was uploaded",
        )
    ):
        return "filled"
    if any(
        tok in lower
        for tok in (
            "job has expired",
            "this job has expired",
            "job posting at the provided url has expired",
            "no longer accepting applications",
            "this job is closed",
        )
    ):
        return "expired"
    if "low confidence" in lower or "too ambiguous" in lower:
        return "low_confidence"
    if any(tok in lower for tok in _CAPTCHA_MARKERS) or "outcome=captcha" in lower:
        return "captcha"
    if "captcha" in lower and any(
        tok in lower for tok in ("blocked by", "hit a", "saw a", "bot wall")
    ):
        return "captcha"
    if any(tok in lower for tok in _SSO_MARKERS):
        return "sso_required"
    if any(tok in lower for tok in _FAILURE_MARKERS):
        return "failed"
    return None


def _history_to_fill_result(
    history: Any,
    *,
    dry_run: bool,
    screenshot_path: str | None = None,
) -> FillResult:
    """Map browser-use AgentHistoryList (or a duck-typed mock) to FillResult."""
    final = ""
    try:
        final = history.final_result() or ""
    except Exception:
        final = ""

    errors: list[Any] = []
    try:
        errors = [e for e in (history.errors() or []) if e]
    except Exception:
        errors = []

    is_successful = False
    try:
        judged = history.is_successful()
        is_successful = bool(judged) if judged is not None else False
    except Exception:
        is_successful = False

    is_done = False
    try:
        is_done = bool(history.is_done())
    except Exception:
        is_done = bool(final)

    outcome = _parse_outcome(final)

    # browser-use's judge may FAIL on a sticky autocomplete while core fields
    # are filled and Submit was never clicked — treat that as dry-run success.
    if (
        outcome is None
        and dry_run
        and is_done
        and _core_fields_filled(final)
        and "submit" not in final.lower().split("outcome=")[-1][:20]
    ):
        outcome = "filled"

    if outcome == "captcha":
        return FillResult(
            status="manual_review",
            error="captcha",
            screenshot_path=screenshot_path,
            adapter=ADAPTER_NAME,
        )
    if outcome == "sso_required":
        return FillResult(
            status="manual_review",
            error="sso_required",
            screenshot_path=screenshot_path,
            adapter=ADAPTER_NAME,
        )
    if outcome == "expired":
        return FillResult(
            status="expired",
            error="expired: job closed / not accepting applications",
            screenshot_path=screenshot_path,
            adapter=ADAPTER_NAME,
        )
    if outcome == "low_confidence":
        # Override only when the narrative clearly shows a usable dry-run fill.
        if dry_run and _core_fields_filled(final):
            return FillResult(
                status="filled",
                screenshot_path=screenshot_path,
                adapter=ADAPTER_NAME,
                fields_skipped=["optional_or_autocomplete"],
            )
        return FillResult(
            status="manual_review",
            error="low_confidence: agent reported ambiguous/incomplete form",
            screenshot_path=screenshot_path,
            adapter=ADAPTER_NAME,
        )
    if outcome == "failed":
        if dry_run and _core_fields_filled(final):
            return FillResult(
                status="filled",
                screenshot_path=screenshot_path,
                adapter=ADAPTER_NAME,
                fields_skipped=["optional_or_autocomplete"],
            )
        return FillResult(
            status="manual_review",
            error=f"tier2_failed: {(final or 'agent reported failure')[:400]}",
            screenshot_path=screenshot_path,
            adapter=ADAPTER_NAME,
        )
    if outcome == "submitted":
        # Honest status even during dry-run: if the agent actually submitted,
        # mark it so we do not try the same posting again.
        return FillResult(
            status="submitted",
            error=(
                "dry_run_violated: agent reported submit"
                if dry_run
                else None
            ),
            screenshot_path=screenshot_path,
            adapter=ADAPTER_NAME,
        )
    if outcome == "filled":
        # Filled ≠ submitted. Submit mode still needs outcome=submitted (or a
        # successful judge when we asked the agent to click Submit).
        return FillResult(
            status="filled",
            screenshot_path=screenshot_path,
            adapter=ADAPTER_NAME,
        )
    if is_done and is_successful and not errors:
        return FillResult(
            status="filled" if dry_run else "submitted",
            screenshot_path=screenshot_path,
            adapter=ADAPTER_NAME,
        )

    detail = (
        final or ("; ".join(str(e) for e in errors[:3])) or "no clear agent outcome"
    )[:400]
    return FillResult(
        status="manual_review",
        error=f"low_confidence: {detail}",
        screenshot_path=screenshot_path,
        adapter=ADAPTER_NAME,
    )


def _collect_history_screenshot_files(history: Any) -> list[Path]:
    """Return existing screenshot files from a browser-use history object."""
    raw: list[Any] = []
    try:
        paths = history.screenshot_paths() if history else None
        if paths:
            raw.extend(paths)
    except Exception:
        pass
    found: list[Path] = []
    seen: set[str] = set()
    for item in raw:
        path = Path(str(item))
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            found.append(path)
    return found


def _persist_screenshot(
    history: Any,
    dest: str | Path | None,
) -> str | None:
    """Copy the agent's last real screenshot into the audit path, if any."""
    sources = _collect_history_screenshot_files(history)
    last = sources[-1] if sources else None
    if dest:
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if last:
            try:
                if last.resolve() != dest_path.resolve():
                    shutil.copyfile(last, dest_path)
                return str(dest_path)
            except OSError as exc:
                logger.warning("Failed to copy Tier 2 screenshot to %s: %s", dest_path, exc)
                return str(last)
        return str(dest_path) if dest_path.is_file() else None
    return str(last) if last else None


def _chrome_executable() -> str | None:
    settings = get_settings()
    channel = str(getattr(settings, "browser_channel", "") or "").strip().lower()
    if channel not in {"chrome", "chromium", "chrome-beta"}:
        return None
    candidates = (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/opt/google/chrome/chrome",
    )
    for path in candidates:
        if Path(path).is_file():
            return path
    return None


def _browser_use_kwargs(
    *,
    headless: bool,
    user_data_dir: str | Path | None,
) -> dict[str, Any]:
    """Build Browser() kwargs, dropping names this browser-use version rejects."""
    from browser_use import Browser

    kwargs: dict[str, Any] = {"headless": headless}
    if user_data_dir:
        kwargs["user_data_dir"] = str(user_data_dir)
    exe = _chrome_executable()
    if exe:
        kwargs["executable_path"] = exe
    try:
        accepted = set(inspect.signature(Browser).parameters)
    except (TypeError, ValueError):
        accepted = set()
    if accepted:
        kwargs = {k: v for k, v in kwargs.items() if k in accepted or k == "headless"}
    return kwargs


def _is_browser_start_timeout(exc: BaseException) -> bool:
    msg = str(exc)
    lowered = msg.lower()
    return "BrowserStartEvent" in msg or (
        "timed out" in lowered and "browser" in lowered
    )


def _tier2_profile_dir(settings: Any) -> Path | None:
    """Dedicated profile for the agent — never the Playwright BROWSER_USER_DATA_DIR."""
    raw = getattr(settings, "tier2_user_data_dir", None)
    if not isinstance(raw, (str, Path)) or raw in {"", None}:
        return None
    path = Path(raw)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return path
    return path


async def _default_run_agent(
    *,
    task: str,
    job_url: str,
    resume_path: str | None,
    max_steps: int,
    headless: bool,
    user_data_dir: str | Path | None,
    cover_letter_path: str | None = None,
    dry_run: bool = True,
) -> Any:
    """Construct and run a real browser-use Agent (lazy import)."""
    settings = get_settings()
    if not os.environ.get("GOOGLE_API_KEY") and settings.gemini_api_key:
        os.environ["GOOGLE_API_KEY"] = settings.gemini_api_key

    from browser_use import Agent, Browser, ChatGoogle

    model = (settings.tier2_llm_model or "").strip() or settings.gemini_model
    llm = ChatGoogle(model=model, api_key=settings.gemini_api_key)

    browser = Browser(**_browser_use_kwargs(headless=headless, user_data_dir=user_data_dir))

    available_files = [p for p in (resume_path, cover_letter_path) if p]
    agent = Agent(
        task=task,
        llm=llm,
        browser=browser,
        available_file_paths=available_files or None,
        use_vision=True,
        calculate_cost=True,
        directly_open_url=True,
        max_actions_per_step=8,
        flash_mode=False,
        extend_system_message=(
            "On job application forms: if you land on a job Overview, click Apply "
            "or open the Application tab first. Fill several visible fields in one "
            "step. Never hunt for profile keys that are not on the page. For location "
            "autocomplete: clear the field, type only a short city name (not full "
            "address), wait for suggestions, click one; if none after two tries, "
            "ArrowDown+Enter or Tab once then move on — never loop on location. "
            "Before done, scroll to the top of the form for the audit screenshot. "
            + (
                "This is a DRY RUN: never click the final Submit button."
                if dry_run
                else "This is a REAL application: after the form is complete, click the final Submit button. Do not stop on Review."
            )
        ),
    )
    _ = job_url  # URL is embedded in the task; Agent navigates itself.
    try:
        return await agent.run(max_steps=max_steps)
    finally:
        closer = getattr(browser, "stop", None) or getattr(browser, "close", None)
        if callable(closer):
            try:
                maybe = closer()
                if hasattr(maybe, "__await__"):
                    await maybe
            except Exception:
                logger.debug("Tier 2 browser close raised", exc_info=True)


def _run_async(coro_factory: Callable[[], Awaitable[Any]]) -> Any:
    """Run an async callable from sync code (CLI has no running loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro_factory())).result()


def run_tier2_apply(
    *,
    job_url: str,
    profile: dict[str, Any],
    materials: dict[str, Any],
    dry_run: bool = True,
    screenshot_path: str | Path | None = None,
    budget: Tier2Budget | None = None,
    agent_runner: Callable[..., Awaitable[Any]] | None = None,
    ats_config: dict[str, Any] | None = None,
) -> FillResult:
    """
    Synchronously run the Tier 2 agent for one job URL.

    `agent_runner` is injectable for unit tests (async callable returning a
    history-like object). Production uses browser-use.
    """
    settings = get_settings()
    cfg = ats_config or load_ats_config()

    if not settings.tier2_enabled:
        return FillResult(
            status="manual_review",
            error="tier2_disabled: enable TIER2_ENABLED to use the generalized agent",
            adapter=ADAPTER_NAME,
        )

    if not materials.get("resume_path"):
        return FillResult(
            status="manual_review",
            error="missing_materials: tailored resume path missing — run tailor stage first",
            adapter=ADAPTER_NAME,
        )

    stop = detect_hard_stop(job_url, "", ats_config=cfg, skip_tier2_ats=True)
    if stop is not None:
        return FillResult(
            status=hard_stop_to_fill_status(stop),
            error=stop.error,
            adapter=ADAPTER_NAME,
        )

    # Consume budget only once we are actually going to call the agent.
    # Remaining jobs stay cover_letter_ready (deferred) so the next apply run
    # can try them — dumping them into manual_review was a false "needs human".
    if budget is not None and not budget.try_consume():
        return FillResult(
            status="deferred",
            error=(
                f"tier2_budget_exhausted: max {budget.max_jobs} Tier 2 jobs per run "
                f"(TIER2_MAX_JOBS_PER_RUN); leaving job queued"
            ),
            adapter=ADAPTER_NAME,
        )

    materials = prepare_tier2_materials(materials)

    task = build_agent_task(
        job_url=job_url,
        profile=profile,
        materials=materials,
        dry_run=dry_run,
    )
    runner = agent_runner or _default_run_agent
    max_steps = settings.tier2_max_agent_steps
    # Tier 2 defaults to headed (TIER2_HEADLESS=false) — independent of BROWSER_HEADLESS.
    raw_headless = getattr(settings, "tier2_headless", False)
    tier2_headless = raw_headless if isinstance(raw_headless, bool) else False
    if tier2_headless:
        logger.warning(
            "Tier 2 running headless (TIER2_HEADLESS=true) — CAPTCHA / bot walls "
            "are more likely; prefer headed for live ATS"
        )
    else:
        logger.info("Tier 2 using headed browser (TIER2_HEADLESS=false)")

    profile_dir = _tier2_profile_dir(settings)

    def _call_runner(udir: str | Path | None) -> Any:
        kwargs: dict[str, Any] = {
            "task": task,
            "job_url": job_url,
            "resume_path": materials.get("resume_path"),
            "max_steps": max_steps,
            "headless": tier2_headless,
            "user_data_dir": udir,
            "cover_letter_path": materials.get("cover_letter_path"),
            "dry_run": dry_run,
        }
        try:
            return _run_async(lambda: runner(**kwargs))
        except TypeError:
            kwargs.pop("cover_letter_path", None)
            kwargs.pop("dry_run", None)
            return _run_async(lambda: runner(**kwargs))

    try:
        history = _call_runner(profile_dir)
    except Exception as exc:
        if _is_browser_start_timeout(exc):
            logger.warning(
                "Tier 2 browser start timed out for %s — retrying with a fresh profile",
                job_url,
            )
            if profile_dir is not None:
                try:
                    from automation.browser import free_persistent_profile

                    free_persistent_profile(Path(profile_dir))
                except Exception:
                    logger.debug("Could not free Tier 2 profile lock", exc_info=True)
            time.sleep(1.5)
            try:
                history = _call_runner(None)
            except Exception as retry_exc:
                logger.error(
                    "Tier 2 agent failed for %s after retry: %s",
                    job_url,
                    retry_exc,
                    exc_info=True,
                )
                return FillResult(
                    status="manual_review",
                    error=f"tier2_error: {retry_exc}",
                    adapter=ADAPTER_NAME,
                )
        else:
            logger.error("Tier 2 agent failed for %s: %s", job_url, exc, exc_info=True)
            return FillResult(
                status="manual_review",
                error=f"tier2_error: {exc}",
                adapter=ADAPTER_NAME,
            )

    shot = _persist_screenshot(history, screenshot_path)
    result = _history_to_fill_result(history, dry_run=dry_run, screenshot_path=shot)

    try:
        urls = list(history.urls() or []) if history else []
        last_url = urls[-1] if urls else job_url
    except Exception:
        last_url = job_url

    post_stop = detect_hard_stop(last_url, "", ats_config=cfg, skip_tier2_ats=True)
    if post_stop is not None and post_stop.code in {
        "sso_required",
        "captcha",
        "unsafe_verification",
        "unsafe_payment",
        "expired",
        "already_applied",
        "site_blocked",
    }:
        return FillResult(
            status=hard_stop_to_fill_status(post_stop),
            error=post_stop.error,
            screenshot_path=result.screenshot_path,
            adapter=ADAPTER_NAME,
        )

    result.adapter = ADAPTER_NAME
    return result


def should_use_tier2(
    url: str,
    *,
    plan_reason: str | None,
    plan_ats: str | None,
    plan_confidence: float,
    adapter_name: str | None,
    form_min_confidence: float,
    ats_config: dict[str, Any] | None = None,
) -> bool:
    """
    Decide whether this job should use the Tier 2 agent instead of Tier 1.

    Tier 2 picks up what would otherwise become manual_review: configured
    tier2_ats domains, and unrecognized / low-confidence generic forms.
    Known Tier 1 adapters (linkedin / workday / greenhouse / confident generic)
    stay on the deterministic path.
    """
    if is_tier2_ats(url, ats_config):
        return True
    if plan_ats in _TIER2_ATS_FAMILIES:
        return True
    if plan_reason in {"manual_ats", "tier2_ats", "unrecognized_form"}:
        return True
    if adapter_name == "generic" and plan_confidence < form_min_confidence:
        return True
    if plan_ats in {"unknown", None} and plan_reason not in {
        "captcha",
        "sso_required",
        None,
    }:
        return True
    return False
