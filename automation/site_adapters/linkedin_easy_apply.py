"""
LinkedIn Easy Apply adapter.

Requires a logged-in LinkedIn session (persistent browser profile). When a login
wall appears in headed mode, the browser stays open so you can sign in; the
session is then reused for later runs via BROWSER_USER_DATA_DIR.

Dry-run fills the modal steps and stops on the Review page — never clicks
"Submit application".
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from automation.form_detector import profile_value_map
from automation.site_adapters import generic as generic_mod
from automation.types import FillResult, FormPlan
from config.settings import get_settings
from utils.logger import get_logger

logger = get_logger("jobapplypro.adapter.linkedin")

FIND_APPLY_SELECTORS = [
    "button.jobs-apply-button",
    "button.jobs-apply-button--top-card",
    ".jobs-s-apply--top-card button",
    ".jobs-s-apply button",
    ".jobs-apply-button",
    "a.jobs-apply-button",
    "button:has-text('Easy Apply')",
    "button:has-text('Apply')",
    "[aria-label*='Easy Apply' i]",
    "[aria-label*='Apply to' i]",
]

LOGIN_WALL_SIGNALS = (
    "sign in to see who you already know",
    "sign in with email",
    "authwall",
    "session_redirect",
    "/checkpoint/lg/",
    "join linkedin",
)

# Weaker signals — only when NOT clearly logged in.
LOGIN_WALL_SOFT_SIGNALS = (
    "sign in",
    "join now",
)

# Positive logged-in markers (feed / nav). These win over soft "sign in" text.
LOGGED_IN_MARKERS = (
    "global-nav__me",
    "feed-identity-module",
    "feed-identity",
    'data-control-name="nav.settings"',
    "msg-overlay-bubble-header",
    "notification-badge",
    "global-nav__primary-link--me",
    'aria-label="me"',
    "share-box-feed-entry",
    "scaffold-layout__main",
)

SUBMIT_SELECTORS = [
    "button:has-text('Submit application')",
    "button[aria-label*='Submit application' i]",
]

NEXT_SELECTORS = [
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "button:has-text('Review')",
]


def looks_like_logged_in(html: str, url: str = "") -> bool:
    """True when the browser session is an authenticated LinkedIn user."""
    url_l = (url or "").lower()
    lower = (html or "").lower()
    if any(
        p in url_l
        for p in ("linkedin.com/feed", "linkedin.com/in/", "/mynetwork", "/messaging")
    ):
        return True
    if "linkedin.com/jobs" in url_l and "authwall" not in url_l and "global-nav" in lower:
        # Job page with top nav usually means logged in.
        if any(m in lower for m in LOGGED_IN_MARKERS) or "global-nav__me" in lower:
            return True
    return any(m in lower for m in LOGGED_IN_MARKERS)


def looks_like_login_wall(html: str, url: str = "") -> bool:
    """True when the page is blocking apply behind LinkedIn auth."""
    if looks_like_logged_in(html, url):
        return False
    lower = (html or "").lower()
    url_l = (url or "").lower()
    if any(s in url_l for s in ("/login", "authwall", "checkpoint/lg", "/uas/login")):
        return True
    if any(s in lower for s in LOGIN_WALL_SIGNALS):
        return True
    has_easy = "easy apply" in lower or "jobs-apply-button" in lower
    if not has_easy and any(s in lower for s in LOGIN_WALL_SOFT_SIGNALS):
        return True
    return False


def session_looks_ready(html: str, url: str = "") -> bool:
    """True when login is done (feed or job page with authenticated chrome)."""
    if looks_like_logged_in(html, url):
        return True
    if looks_like_login_wall(html, url):
        return False
    lower = (html or "").lower()
    if "easy apply" in lower or "jobs-apply-button" in lower:
        return True
    return False


def _page_looks_logged_in(page) -> bool:
    """DOM/locator fallback — LinkedIn class names change; URL+markers first."""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    try:
        html = page.content()
    except Exception:
        html = ""
    if looks_like_logged_in(html, url):
        return True
    # Locators that only exist when authenticated
    for sel in (
        ".global-nav__me",
        "#global-nav",
        "button.global-nav__primary-link-me-menu-trigger",
        ".feed-identity-module",
        "[data-test-global-nav-link='feed']",
    ):
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def wait_for_manual_login(
    page,
    *,
    timeout_sec: int,
    job_url: str | None = None,
    poll_sec: float = 2.0,
) -> bool:
    """
    Keep the headed browser open so the user can sign in to LinkedIn.
    Returns True if the session looks ready before timeout.
    """
    msg = (
        f"\n*** LinkedIn login required ***\n"
        f"Sign in in the open browser window "
        f"(waiting up to {timeout_sec}s). Session will be saved to your profile.\n"
        f"After you land on your Home/Jobs feed, we will reopen the job link.\n"
    )
    print(msg, flush=True)
    logger.info(
        "Waiting up to %ss for manual LinkedIn login (headed mode)", timeout_sec
    )

    deadline = time.time() + max(timeout_sec, 1)
    while time.time() < deadline:
        try:
            if _page_looks_logged_in(page) or session_looks_ready(
                "", getattr(page, "url", "") or ""
            ):
                # Re-check with HTML for soft false positives
                try:
                    html = page.content()
                    url = page.url or ""
                except Exception:
                    html, url = "", ""
                if looks_like_logged_in(html, url) or _page_looks_logged_in(page):
                    logger.info("LinkedIn session looks ready (url=%s)", url)
                    if job_url:
                        try:
                            page.goto(
                                job_url, wait_until="domcontentloaded", timeout=60000
                            )
                            try:
                                page.wait_for_timeout(1500)
                            except Exception:
                                pass
                        except Exception as exc:
                            logger.warning(
                                "Could not reload job URL after login: %s", exc
                            )
                    return True
        except Exception as exc:
            logger.debug("Login wait poll error: %s", exc)

        remaining = int(deadline - time.time())
        if remaining > 0 and remaining % 30 < poll_sec:
            logger.info("Still waiting for LinkedIn login… %ss left", remaining)
        try:
            page.wait_for_timeout(int(poll_sec * 1000))
        except Exception:
            time.sleep(poll_sec)

    logger.warning("Timed out waiting for LinkedIn login")
    return False


class LinkedInEasyApplyAdapter:
    name = "linkedin_easy_apply"

    def matches(self, url: str, html: str = "") -> bool:
        host = (urlparse(url).hostname or "").lower()
        if "linkedin.com" not in host:
            return False
        lower = (html or "").lower()
        if not lower:
            return True
        return "easy apply" in lower or "jobs-apply" in lower or "/jobs/" in url.lower()

    def fill(
        self,
        page,
        plan: FormPlan,
        profile: dict[str, Any],
        materials: dict[str, Any],
        *,
        dry_run: bool = True,
    ) -> FillResult:
        settings = get_settings()
        job_url = materials.get("job_url")

        try:
            html = page.content()
            current_url = ""
            try:
                current_url = page.url or ""
            except Exception:
                current_url = job_url or ""
        except Exception as exc:
            return FillResult(
                status="failed",
                error=f"Could not read LinkedIn page: {exc}",
                adapter=self.name,
            )

        if looks_like_login_wall(html, current_url):
            if settings.browser_headless:
                return FillResult(
                    status="manual_review",
                    error=(
                        "login_required: LinkedIn auth wall — set BROWSER_HEADLESS=false "
                        "and BROWSER_USER_DATA_DIR, then re-run to sign in"
                    ),
                    adapter=self.name,
                )

            ok = wait_for_manual_login(
                page,
                timeout_sec=settings.browser_login_wait_seconds,
                job_url=job_url or current_url,
            )
            if not ok:
                return FillResult(
                    status="manual_review",
                    error=(
                        "login_required: timed out waiting for LinkedIn sign-in "
                        f"({settings.browser_login_wait_seconds}s)"
                    ),
                    adapter=self.name,
                )
            try:
                html = page.content()
            except Exception as exc:
                return FillResult(
                    status="failed",
                    error=f"Could not read LinkedIn page after login: {exc}",
                    adapter=self.name,
                )
            if looks_like_login_wall(html, job_url or current_url):
                return FillResult(
                    status="manual_review",
                    error="login_required: LinkedIn auth wall still present after wait",
                    adapter=self.name,
                )

        # Wait briefly for the Apply control (LinkedIn hydrates slowly).
        _wait_for_apply_button(page, timeout_ms=12000)

        apply_page, apply_kind = _click_apply(page)
        if apply_page is None:
            return FillResult(
                status="manual_review",
                error=(
                    "linkedin_apply_not_found: no visible Easy Apply / Apply button "
                    "(job may have closed or UI changed)"
                ),
                adapter=self.name,
            )

        # External company ATS (e.g. "Responses managed off LinkedIn").
        if apply_kind == "external":
            logger.info(
                "LinkedIn external apply detected — handing off to ATS adapter (%s)",
                getattr(apply_page, "url", ""),
            )
            return _fill_external_ats(
                apply_page, profile, materials, dry_run=dry_run
            )

        # In-LinkedIn Easy Apply modal on the original page.
        return _fill_easy_apply_modal(
            apply_page, plan, profile, materials, dry_run=dry_run
        )


def _wait_for_apply_button(page, *, timeout_ms: int = 15000) -> None:
    """Wait until any Apply / Easy Apply control is visible."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        loc, _ = _find_apply_locator(page)
        if loc is not None:
            return
        try:
            page.wait_for_timeout(400)
        except Exception:
            time.sleep(0.4)
    logger.debug("Apply button not visible within %sms", timeout_ms)


def _is_plausible_apply_label(text: str) -> bool:
    """Reject sidebar job cards / marketing CTAs that merely contain 'apply'."""
    low = (text or "").strip().lower()
    if "apply" not in low:
        return False
    # Real controls are short: "Apply", "Easy Apply", "Apply to Acme".
    if len(low) > 48 or low.count("\n") > 0:
        return False
    if any(
        bad in low
        for bad in (
            "tailor",
            "show match",
            "post a job",
            "premium",
            "actively reviewing",
            "remote)",
            "people clicked",
        )
    ):
        return False
    return True


def _find_apply_locator(page):
    """
    Return (locator, label_text) for the top-card Apply / Easy Apply control.

    LinkedIn often uses artdeco buttons with nested spans + an external-link icon,
    so role/name matching is more reliable than a single CSS selector.
    """
    import re as _re

    # 0) Prefer the job-details top card Apply button (avoids similar-jobs cards).
    for sel in (
        ".jobs-apply-button--top-card button",
        ".jobs-apply-button--top-card a",
        "button.jobs-apply-button",
        "div.jobs-s-apply button",
        "div.jobs-s-apply a",
    ):
        try:
            loc = page.locator(sel)
            count = loc.count()
            for i in range(min(count, 4)):
                candidate = loc.nth(i)
                try:
                    if not candidate.is_visible(timeout=600):
                        continue
                    text = (candidate.inner_text(timeout=600) or "").strip()
                    if _is_plausible_apply_label(text):
                        logger.info(
                            "Found LinkedIn apply control via top-card css=%s text=%r",
                            sel,
                            text[:80],
                        )
                        return candidate, text
                except Exception:
                    continue
        except Exception:
            continue

    # 1) Role / accessible name (best for current LinkedIn UI)
    role_patterns = (
        r"^\s*Easy Apply\s*$",
        r"^\s*Apply\s*$",
        r"Easy Apply",
        r"^Apply\b",
        r"Apply to .+",
    )
    for pattern in role_patterns:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=_re.compile(pattern, _re.I))
                count = loc.count()
                if count == 0:
                    continue
                for i in range(min(count, 5)):
                    candidate = loc.nth(i)
                    try:
                        if not candidate.is_visible(timeout=800):
                            continue
                        text = (candidate.inner_text(timeout=800) or "").strip()
                        if _is_plausible_apply_label(text):
                            logger.info(
                                "Found LinkedIn apply control via role=%s text=%r",
                                role,
                                text[:80],
                            )
                            return candidate, text
                    except Exception:
                        continue
            except Exception:
                continue

    # 2) CSS fallbacks — check every match, not only .first
    for sel in FIND_APPLY_SELECTORS:
        try:
            loc = page.locator(sel)
            count = loc.count()
            if count == 0:
                continue
            for i in range(min(count, 8)):
                candidate = loc.nth(i)
                try:
                    if not candidate.is_visible(timeout=600):
                        continue
                    text = (candidate.inner_text(timeout=600) or "").strip()
                    if not _is_plausible_apply_label(text):
                        continue
                    logger.info(
                        "Found LinkedIn apply control via css=%s text=%r",
                        sel,
                        text[:80],
                    )
                    return candidate, text
                except Exception:
                    continue
        except Exception:
            continue

    # 3) Last resort: any visible element whose normalized text is Apply / Easy Apply
    try:
        for text_label in ("Easy Apply", "Apply"):
            loc = page.get_by_text(text_label, exact=True)
            count = loc.count()
            for i in range(min(count, 5)):
                candidate = loc.nth(i)
                try:
                    if candidate.is_visible(timeout=500):
                        # Prefer clicking the button ancestor if text node is a span.
                        btn = candidate.locator(
                            "xpath=ancestor-or-self::button[1] | ancestor-or-self::a[1]"
                        )
                        if btn.count() > 0:
                            return btn.first, text_label
                        return candidate, text_label
                except Exception:
                    continue
    except Exception:
        pass

    return None, ""


def _click_apply(page) -> tuple[Any, str]:
    """
    Click Apply / Easy Apply.

    Returns (page_to_fill, kind) where kind is 'easy' | 'external'.
    page_to_fill may be a new tab for external ATS.

    Important: do NOT wrap the click in expect_page alone — when no tab opens
    (same-tab nav or Easy Apply modal), expect_page times out and previously
    made us treat a successful click as total failure.
    """
    loc, text = _find_apply_locator(page)
    if loc is None:
        logger.warning("No LinkedIn Apply / Easy Apply control found on page")
        return None, ""

    label = (text or "").lower()
    kind = "easy" if "easy apply" in label else "external"
    try:
        html = (page.content() or "").lower()
        if "responses managed off linkedin" in html:
            kind = "external"
        elif "easy apply" in label:
            kind = "easy"
    except Exception:
        pass

    context = page.context
    try:
        pages_before = list(context.pages)
    except Exception:
        pages_before = []
    try:
        url_before = page.url or ""
    except Exception:
        url_before = ""

    try:
        loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass

    try:
        # force=True helps when LinkedIn's messaging dock / overlays intercept.
        loc.click(timeout=8000, force=True)
        logger.info("Clicked LinkedIn apply control (kind=%s, label=%r)", kind, text[:60])
    except Exception as exc:
        logger.warning("Apply click failed: %s", exc)
        return None, ""

    # Give LinkedIn a moment to open a tab or navigate.
    try:
        page.wait_for_timeout(2000)
    except Exception:
        time.sleep(2)

    # New tab opened?
    try:
        pages_after = list(context.pages)
    except Exception:
        pages_after = pages_before
    if len(pages_after) > len(pages_before):
        new_page = pages_after[-1]
        try:
            new_page.wait_for_load_state("domcontentloaded", timeout=25000)
            # Career sites inject OneTrust + Greenhouse iframes after first paint.
            new_page.wait_for_timeout(2500)
        except Exception:
            pass
        logger.info("External apply opened new tab: %s", getattr(new_page, "url", ""))
        return new_page, "external"

    # Same-tab navigation away from LinkedIn?
    try:
        url_after = page.url or ""
    except Exception:
        url_after = url_before
    if url_after and "linkedin.com" not in url_after.lower():
        logger.info("External apply navigated same tab to %s", url_after)
        return page, "external"

    # Popup may open slightly later — one more short poll.
    try:
        page.wait_for_timeout(2000)
        pages_after = list(context.pages)
        if len(pages_after) > len(pages_before):
            new_page = pages_after[-1]
            try:
                new_page.wait_for_load_state("domcontentloaded", timeout=25000)
            except Exception:
                pass
            return new_page, "external"
        url_after = page.url or url_after
        if url_after and "linkedin.com" not in url_after.lower():
            return page, "external"
    except Exception:
        pass

    if kind == "external":
        logger.warning(
            "External Apply clicked but still on LinkedIn (%s)", url_after or url_before
        )
        return page, "external"
    return page, "easy"


def _fill_external_ats(
    page,
    profile: dict[str, Any],
    materials: dict[str, Any],
    *,
    dry_run: bool,
) -> FillResult:
    from automation.cookies import dismiss_cookie_banners
    from automation.form_detector import build_form_plan
    from automation.site_adapters import greenhouse as greenhouse_mod
    from automation.site_adapters import workday as workday_mod

    try:
        url = page.url or ""
    except Exception:
        url = ""

    # Still on LinkedIn after "external" Apply → can't fill company ATS here.
    if "linkedin.com" in url.lower():
        return FillResult(
            status="manual_review",
            error=(
                "linkedin_external_apply: job uses off-LinkedIn Apply "
                "(Responses managed off LinkedIn) but no company ATS tab opened"
            ),
            adapter="linkedin_external",
        )

    # Cookie CMP (OneTrust etc.) often injects after first paint.
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass
    dismiss_cookie_banners(page)
    try:
        page.wait_for_timeout(1000)
    except Exception:
        pass

    try:
        html = page.content()
    except Exception as exc:
        return FillResult(
            status="manual_review",
            error=f"linkedin_external_apply: could not read ATS page: {exc}",
            adapter="linkedin_external",
        )

    plan = build_form_plan(url, html, page=page)
    if greenhouse_mod.adapter.matches(url, html):
        result = greenhouse_mod.adapter.fill(
            page, plan, profile, materials, dry_run=dry_run
        )
    elif workday_mod.adapter.matches(url, html):
        result = workday_mod.adapter.fill(
            page, plan, profile, materials, dry_run=dry_run
        )
    else:
        result = generic_mod.adapter.fill(
            page, plan, profile, materials, dry_run=dry_run
        )
    result.adapter = f"linkedin_external->{result.adapter or 'generic'}"
    return result


def _fill_easy_apply_modal(
    page,
    plan: FormPlan,
    profile: dict[str, Any],
    materials: dict[str, Any],
    *,
    dry_run: bool,
) -> FillResult:
    values = profile_value_map(profile)
    filled: list[str] = []
    skipped: list[str] = []

    # New LinkedIn Easy Apply UI often mounts contact fields a beat after click.
    _wait_for_easy_apply_contact_fields(page)

    contact_filled, contact_skipped = _fill_easy_apply_contact_fields(page, values)
    filled.extend(contact_filled)
    skipped.extend(contact_skipped)

    resume_path = materials.get("resume_path")
    uploadable: Path | None = None
    if resume_path and Path(resume_path).exists():
        try:
            from automation.resume_upload import ensure_uploadable_resume

            uploadable = ensure_uploadable_resume(resume_path)
        except Exception as exc:
            logger.warning("Easy Apply: resume convert failed: %s", exc)
            skipped.append("resume")

    if uploadable is not None:
        if _upload_easy_apply_resume(page, uploadable):
            filled.append("resume")

    for _ in range(8):
        # Only re-fill contact while those fields are still on-screen.
        if _easy_apply_contact_fields_visible(page):
            more_filled, more_skipped = _fill_easy_apply_contact_fields(page, values)
            for name in more_filled:
                if name not in filled:
                    filled.append(name)
            for name in more_skipped:
                if name not in filled and name not in skipped:
                    skipped.append(name)

        # Resume step often appears after Next from contact info.
        if uploadable is not None and "resume" not in filled:
            if _upload_easy_apply_resume(page, uploadable):
                filled.append("resume")

        advanced = False
        for sel in NEXT_SELECTORS:
            try:
                loc = page.locator(sel)
                if loc.count() == 0:
                    continue
                label = ""
                try:
                    label = (loc.first.inner_text(timeout=1000) or "").lower()
                except Exception:
                    label = ""
                if "submit" in label:
                    continue
                # Don't advance past resume step until upload succeeded.
                if (
                    uploadable is not None
                    and "resume" not in filled
                    and _easy_apply_resume_step_visible(page)
                ):
                    if _upload_easy_apply_resume(page, uploadable):
                        filled.append("resume")
                    else:
                        logger.warning(
                            "Easy Apply: resume still required — not clicking %r",
                            label or sel,
                        )
                        continue
                loc.first.click(timeout=3000)
                advanced = True
                try:
                    page.wait_for_timeout(600)
                except Exception:
                    pass
                break
            except Exception:
                continue

        if dry_run:
            for sel in SUBMIT_SELECTORS:
                try:
                    if page.locator(sel).count() > 0 and not advanced:
                        logger.info("Dry-run: stopping on LinkedIn review/submit step")
                        return FillResult(
                            status="filled",
                            fields_filled=filled,
                            fields_skipped=skipped,
                            adapter="linkedin_easy_apply",
                        )
                except Exception:
                    continue

        if not dry_run:
            for sel in SUBMIT_SELECTORS:
                try:
                    page.click(sel, timeout=3000)
                    return FillResult(
                        status="submitted",
                        fields_filled=filled,
                        fields_skipped=skipped,
                        adapter="linkedin_easy_apply",
                    )
                except Exception:
                    continue

        if not advanced:
            break

    if plan.fields:
        plan_copy = FormPlan(
            ats=plan.ats,
            fields=plan.fields,
            apply_button=None,
            submit_button=None,
            multi_page=False,
            confidence=plan.confidence,
        )
        extra = generic_mod.adapter.fill(
            page, plan_copy, profile, materials, dry_run=True
        )
        filled = list(dict.fromkeys(filled + extra.fields_filled))
        skipped = list(dict.fromkeys(skipped + extra.fields_skipped))

    if dry_run:
        return FillResult(
            status="filled",
            fields_filled=filled,
            fields_skipped=skipped,
            adapter="linkedin_easy_apply",
        )

    return FillResult(
        status="manual_review",
        error="linkedin_submit_not_reached",
        fields_filled=filled,
        fields_skipped=skipped,
        adapter="linkedin_easy_apply",
    )


def _wait_for_easy_apply_contact_fields(page, *, timeout_ms: int = 8000) -> None:
    """Wait until Easy Apply contact inputs appear (new UI uses type=tel)."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        if _easy_apply_contact_fields_visible(page):
            return
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)


def _easy_apply_contact_fields_visible(page) -> bool:
    selectors = (
        "input[type='tel']",
        "input[name='phoneNumber']",
        "input[name='email']",
        "input[type='email']",
    )
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=300):
                return True
        except Exception:
            continue
    return False


def _easy_apply_resume_step_visible(page) -> bool:
    """True when the Easy Apply modal is asking for a resume upload/selection."""
    try:
        if page.get_by_role("button", name="Upload resume").count() > 0:
            btn = page.get_by_role("button", name="Upload resume").first
            if btn.is_visible(timeout=500):
                return True
    except Exception:
        pass
    for sel in (
        "button:has-text('Upload resume')",
        "text=A resume is required",
        "text=/Select or upload a resume/i",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=400):
                return True
        except Exception:
            continue
    return False


def _upload_easy_apply_resume(page, resume_path: Path) -> bool:
    """
    Upload a DOC/DOCX/PDF via LinkedIn's Easy Apply 'Upload resume' control.

    New Easy Apply UI often has no pre-mounted file input — clicking Upload
    opens a file chooser. Fall back to set_input_files when an input exists.
    """
    path = Path(resume_path)
    if not path.exists():
        return False

    # Already uploaded? Look for the filename or a selected resume card.
    try:
        stem = path.stem.lower()
        body = (page.content() or "").lower()
        if stem and stem[:24] in body and "resume is required" not in body:
            # Heuristic: our file name appears and error is gone.
            if page.locator("text=A resume is required").count() == 0:
                logger.info("Easy Apply: resume already present (%s)", path.name)
                return True
    except Exception:
        pass

    # Prefer file-chooser flow (current LinkedIn UI).
    upload_btn = None
    for finder in (
        lambda: page.get_by_role("button", name="Upload resume"),
        lambda: page.get_by_role("button", name=re.compile(r"Upload resume", re.I)),
        lambda: page.locator("button:has-text('Upload resume')"),
    ):
        try:
            loc = finder()
            if loc.count() > 0 and loc.first.is_visible(timeout=800):
                upload_btn = loc.first
                break
        except Exception:
            continue

    if upload_btn is not None:
        try:
            with page.expect_file_chooser(timeout=8000) as fc_info:
                upload_btn.click(timeout=4000)
            chooser = fc_info.value
            chooser.set_files(str(path))
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
            # Confirm error cleared when possible.
            try:
                err = page.locator("text=A resume is required")
                if err.count() == 0 or not err.first.is_visible(timeout=500):
                    logger.info("Easy Apply: uploaded resume via file chooser (%s)", path.name)
                    return True
            except Exception:
                logger.info("Easy Apply: uploaded resume via file chooser (%s)", path.name)
                return True
            logger.warning("Easy Apply: upload clicked but resume still required")
        except Exception as exc:
            logger.info("Easy Apply: file-chooser upload failed (%s) — trying input", exc)

    # Fallback: direct file input (legacy / after chooser mounts input).
    for sel in (
        "input[type='file']",
        "input[accept*='pdf' i]",
        "input[accept*='msword' i]",
        "input[name*='file' i]",
    ):
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            loc.first.set_input_files(str(path), timeout=5000)
            try:
                page.wait_for_timeout(1200)
            except Exception:
                pass
            logger.info("Easy Apply: uploaded resume via %s (%s)", sel, path.name)
            return True
        except Exception:
            continue

    return False


def _fill_easy_apply_contact_fields(
    page, values: dict[str, str]
) -> tuple[list[str], list[str]]:
    """
    Fill Easy Apply contact fields.

    LinkedIn's 2025+ Easy Apply UI uses opaque ids and label association
    (e.g. input[type=tel] labeled "Mobile phone number") instead of
    name='phoneNumber'. Prefer labels, then type=tel/email, then legacy names.
    """
    filled: list[str] = []
    skipped: list[str] = []

    phone = _normalize_phone(values.get("phone") or "")
    email = (values.get("email") or "").strip()
    first = (values.get("first_name") or "").strip()
    last = (values.get("last_name") or "").strip()

    if phone:
        if _fill_by_strategies(
            page,
            phone,
            label_names=(
                "Mobile phone number",
                "Phone number",
                "Mobile phone",
                "Phone",
            ),
            css_selectors=(
                "input[type='tel']",
                "input[name='phoneNumber']",
                "input[id*='phoneNumber' i]",
                "input[id*='nationalNumber' i]",
                "input[id*='phone' i]",
                "input[autocomplete='tel']",
                "input[autocomplete='tel-national']",
            ),
        ):
            filled.append("phone")
            logger.info("Easy Apply: filled phone")
        else:
            skipped.append("phone")
            logger.warning("Easy Apply: could not fill phone field")
    else:
        skipped.append("phone")
        logger.warning("Easy Apply: profile phone is empty")

    if email:
        if _fill_by_strategies(
            page,
            email,
            label_names=("Email address", "Email"),
            css_selectors=(
                "input[type='email']",
                "input[name='email']",
                "input[id*='email' i]",
                "input[autocomplete='email']",
            ),
        ):
            filled.append("email")
        else:
            skipped.append("email")

    if first:
        if _fill_by_strategies(
            page,
            first,
            label_names=("First name",),
            css_selectors=("input[name='firstName']", "input[id*='firstName' i]"),
        ):
            filled.append("first_name")
    if last:
        if _fill_by_strategies(
            page,
            last,
            label_names=("Last name",),
            css_selectors=("input[name='lastName']", "input[id*='lastName' i]"),
        ):
            filled.append("last_name")

    return filled, skipped


def _normalize_phone(raw: str) -> str:
    """Keep digits (and leading +) so LinkedIn tel inputs accept the value."""
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("+"):
        digits = "".join(ch for ch in s[1:] if ch.isdigit())
        return f"+{digits}" if digits else ""
    return "".join(ch for ch in s if ch.isdigit())


def _fill_by_strategies(
    page,
    value: str,
    *,
    label_names: tuple[str, ...],
    css_selectors: tuple[str, ...],
) -> bool:
    """Try label-based fill first, then CSS selectors. Returns True on success."""
    for label in label_names:
        try:
            loc = page.get_by_label(label, exact=False)
            if loc.count() == 0:
                continue
            candidate = loc.first
            if not candidate.is_visible(timeout=800):
                continue
            candidate.click(timeout=1500)
            candidate.fill("", timeout=1500)
            candidate.fill(value, timeout=2500)
            # Confirm the value stuck (React-controlled inputs sometimes need type).
            try:
                current = (candidate.input_value(timeout=800) or "").strip()
            except Exception:
                current = ""
            if not current:
                candidate.press_sequentially(value, delay=20)
            logger.debug("Filled via label %r", label)
            return True
        except Exception:
            continue

    for sel in css_selectors:
        try:
            loc = page.locator(sel)
            count = loc.count()
            if count == 0:
                continue
            for i in range(min(count, 3)):
                candidate = loc.nth(i)
                try:
                    if not candidate.is_visible(timeout=600):
                        continue
                    candidate.click(timeout=1500)
                    candidate.fill("", timeout=1500)
                    candidate.fill(value, timeout=2500)
                    try:
                        current = (candidate.input_value(timeout=800) or "").strip()
                    except Exception:
                        current = ""
                    if not current:
                        candidate.press_sequentially(value, delay=20)
                    logger.debug("Filled via css %s", sel)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


adapter = LinkedInEasyApplyAdapter()
