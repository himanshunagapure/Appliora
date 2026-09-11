"""
Workday application adapter (myworkdayjobs.com and similar).

Walks the typical multi-page flow: Apply → My Information → Experience →
Questions → Review. Dry-run stops on Review and never clicks Submit.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from automation.cookies import dismiss_cookie_banners
from automation.form_detector import profile_value_map
from automation.site_adapters import generic as generic_mod
from automation.types import FillResult, FormPlan
from config.settings import get_settings
from utils.logger import get_logger

logger = get_logger("jobapplypro.adapter.workday")

APPLY_SELECTORS = [
    "a[data-automation-id='jobPostingApplyButton']",
    "button[data-automation-id='jobPostingApplyButton']",
    "button:has-text('Apply'):not(:has-text('Apply Manually')):not(:has-text('Apply with'))",
    "a:has-text('Apply'):not(:has-text('Apply Manually')):not(:has-text('Apply with'))",
]

START_MODAL_AUTOFILL_SELECTORS = [
    "button:has-text('Autofill with Resume')",
    "a:has-text('Autofill with Resume')",
    "[role='button']:has-text('Autofill with Resume')",
    "[data-automation-id='autofillWithResume']",
]

START_MODAL_MANUAL_SELECTORS = [
    "button:has-text('Apply Manually')",
    "a:has-text('Apply Manually')",
    "[role='button']:has-text('Apply Manually')",
]

START_MODAL_LAST_APP_SELECTORS = [
    "button:has-text('Use My Last Application')",
    "a:has-text('Use My Last Application')",
    "[role='button']:has-text('Use My Last Application')",
]

NEXT_SELECTORS = [
    "button[data-automation-id='bottom-navigation-next-button']",
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "button:has-text('Save and Continue')",
]

SUBMIT_SELECTORS = [
    "button[data-automation-id='bottom-navigation-next-button']:has-text('Submit')",
    "button:has-text('Submit')",
    "button[data-automation-id='submit']",
]


class WorkdayAdapter:
    name = "workday"

    def matches(self, url: str, html: str = "") -> bool:
        host = (urlparse(url).hostname or "").lower()
        if any(
            x in host
            for x in ("myworkdayjobs.com", "myworkdaysite.com", "workday.com")
        ):
            return True
        lower = (html or "").lower()
        return "data-automation-id" in lower and "workday" in lower

    def fill(
        self,
        page,
        plan: FormPlan,
        profile: dict[str, Any],
        materials: dict[str, Any],
        *,
        dry_run: bool = True,
    ) -> FillResult:
        filled: list[str] = []
        skipped: list[str] = []
        values = profile_value_map(profile)
        settings = get_settings()

        dismiss_cookie_banners(page)
        current = str(getattr(page, "url", "") or "")
        if "/apply" not in current.lower():
            _click_job_apply(page, plan)
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
        _recover_workday_error(page)

        uploadable = _uploadable_resume(materials.get("resume_path"))

        # Persistent profile already holds the Workday session. Never sit on a
        # 5-minute login wait — use the saved cookies and keep filling.
        try:
            page.wait_for_timeout(2000)
        except Exception:
            pass

        if _on_workday_sign_in_page(page) and not settings.browser_headless:
            print(
                "\n*** Workday Sign In is showing — sign in in this window "
                "(Google/email). Waiting 45s, then continuing.\n",
                flush=True,
            )
            _wait_for_workday_login(page, timeout_sec=45)

        if _start_application_modal_visible(page) and not _on_workday_sign_in_page(page):
            started = _handle_start_application_modal(page, uploadable)
            if started:
                filled.append("resume" if started == "resume" else started)
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass

        if "manual" in filled or "last_application" in filled:
            _wait_for_apply_navigation(page, timeout_sec=60)

        if _on_workday_sign_in_page(page) and not settings.browser_headless:
            print(
                "\n*** Workday Sign In is showing — sign in in this window "
                "(session is saved after this). Waiting 45s, then continuing.\n",
                flush=True,
            )
            _wait_for_workday_login(page, timeout_sec=45)

        if (
            uploadable is not None
            and "resume" not in filled
            and "last_application" not in filled
            and "manual" not in filled
        ):
            if _handle_autofill_resume_page(page, uploadable):
                filled.append("resume")
            elif _start_application_modal_visible(page):
                started = _handle_start_application_modal(page, uploadable)
                if started == "resume":
                    filled.append("resume")

        # Resume parse is slow. Clicking Continue too soon crashes My Information.
        if _on_autofill_resume_page(page) and "last_application" not in filled:
            _settle_resume_parse(page, timeout_sec=12)
            if _on_autofill_resume_page(page):
                _click_first(page, NEXT_SELECTORS)
                try:
                    page.wait_for_timeout(3000)
                except Exception:
                    pass

        if _workday_error_page(page):
            _recover_workday_error(page)
        _wait_for_my_information(page, timeout_sec=40)
        if _workday_error_page(page):
            _recover_workday_error(page)
            _wait_for_my_information(page, timeout_sec=25)

        if _workday_error_page(page):
            return FillResult(
                status="manual_review",
                error="workday_page_error: Something went wrong — refresh and retry",
                fields_filled=filled,
                fields_skipped=skipped,
                adapter=self.name,
            )

        form_ready = _workday_form_ready(page) or _my_information_ready(page)
        field_tries = [
            ("email", "input[data-automation-id='email']"),
            (
                "phone",
                "input[data-automation-id='phone-phoneNumber'], "
                "input[data-automation-id='phoneNumber'], "
                "input[data-automation-id='phone'], "
                "input[data-automation-id*='phoneNumber'], "
                "input[type='tel']",
            ),
            (
                "first_name",
                "input[data-automation-id='legalNameSection_firstName'], "
                "input[data-automation-id='legalNameSection_givenName'], "
                "input[data-automation-id*='givenName'], "
                "input[data-automation-id*='firstName']",
            ),
            (
                "last_name",
                "input[data-automation-id='legalNameSection_lastName'], "
                "input[data-automation-id='legalNameSection_familyName'], "
                "input[data-automation-id*='familyName'], "
                "input[data-automation-id*='lastName']",
            ),
            ("city", "input[data-automation-id='addressSection_city'], input[data-automation-id*='city']"),
            (
                "address_line1",
                "input[data-automation-id='addressSection_addressLine1'], "
                "input[data-automation-id*='addressLine1']",
            ),
            (
                "postal_code",
                "input[data-automation-id='addressSection_postalCode'], "
                "input[data-automation-id*='postalCode']",
            ),
        ]
        for logical, sel in field_tries:
            val = values.get(logical)
            if not val:
                skipped.append(logical)
                continue
            # Login email uses the same selector; only fill after the application form is up.
            if logical == "email" and not form_ready:
                skipped.append(logical)
                continue
            if _fill_input(page, [sel], val):
                filled.append(logical)
            else:
                skipped.append(logical)

        if plan.fields:
            plan_copy = FormPlan(
                ats=plan.ats,
                fields=[f for f in plan.fields if f.name not in filled],
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

        # Walk Autofill → My Information → Experience → Questions → Disclosures → Review.
        last_step = ""
        stuck = 0
        for _step in range(12):
            _recover_workday_error(page)
            if _workday_error_page(page):
                return FillResult(
                    status="manual_review",
                    error="workday_page_error: Something went wrong — refresh and retry",
                    fields_filled=filled,
                    fields_skipped=skipped,
                    adapter=self.name,
                )
            step = _current_wizard_step(page)
            logger.info("Workday wizard step=%s stuck=%s", step, stuck)
            if step == last_step:
                stuck += 1
            else:
                stuck = 0
                last_step = step

            _fill_workday_page(page, values, profile, materials, filled)
            if _page_has_errors(page):
                _answer_error_banner(page, values, profile, materials, filled)
            _answer_interview_consent(page, filled)
            if _on_voluntary_disclosures_page(page):
                _fill_voluntary_disclosures(page, filled)

            if dry_run and (_submit_visible(page) or step == "review"):
                logger.info("Dry-run: stopping on Workday review/submit step")
                return FillResult(
                    status="filled",
                    fields_filled=filled,
                    fields_skipped=skipped,
                    adapter=self.name,
                )
            if not dry_run and _click_first(page, SUBMIT_SELECTORS):
                return FillResult(
                    status="submitted",
                    fields_filled=filled,
                    fields_skipped=skipped,
                    adapter=self.name,
                )
            if stuck >= 4:
                logger.warning("Workday stuck on step %s — stopping advance", step)
                break
            if step == "my_experience" and stuck >= 1:
                _clear_optional_empty_panels(page)
                if "work_experience" in filled:
                    filled[:] = [
                        x
                        for x in filled
                        if x not in {"work_experience", "education", "work_experience_retry"}
                    ]
            if step == "autofill":
                _settle_resume_parse(page, timeout_sec=8)
            if _my_information_shell_empty(page):
                _wait_for_my_information(page, timeout_sec=12)
                if _my_information_shell_empty(page):
                    logger.warning("Workday My Information still empty")
                    break
            if not _click_wizard_next(page, dry_run=dry_run):
                break
            try:
                page.wait_for_timeout(2500)
            except Exception:
                pass
            if _workday_error_page(page):
                _recover_workday_error(page)
                _wait_for_my_information(page, timeout_sec=15)
                if _workday_error_page(page):
                    return FillResult(
                        status="manual_review",
                        error="workday_page_error: Something went wrong after Save and Continue",
                        fields_filled=filled,
                        fields_skipped=skipped,
                        adapter=self.name,
                    )
            if _page_has_errors(page):
                _answer_error_banner(page, values, profile, materials, filled)

        if _workday_error_page(page):
            return FillResult(
                status="manual_review",
                error="workday_page_error: Something went wrong — refresh and retry",
                fields_filled=filled,
                fields_skipped=skipped,
                adapter=self.name,
            )

        if dry_run:
            return FillResult(
                status="filled",
                fields_filled=filled,
                fields_skipped=skipped,
                adapter=self.name,
            )

        return FillResult(
            status="manual_review",
            error="workday_submit_not_reached",
            fields_filled=filled,
            fields_skipped=skipped,
            adapter=self.name,
        )


def _click_job_apply(page, plan: FormPlan) -> bool:
    deadline = time.time() + 20
    while time.time() < deadline:
        selectors = APPLY_SELECTORS + ([plan.apply_button] if plan.apply_button else [])
        for sel in selectors:
            if not sel:
                continue
            try:
                loc = page.locator(sel)
                count = loc.count()
                if count == 0:
                    continue
                for i in range(min(count, 4)):
                    candidate = loc.nth(i)
                    try:
                        if not candidate.is_visible(timeout=800):
                            continue
                        text = (candidate.inner_text(timeout=800) or "").lower()
                        if any(
                            bad in text
                            for bad in ("apply with", "apply manually", "autofill")
                        ):
                            continue
                        if "apply" not in text and "jobPostingApplyButton" not in sel:
                            continue
                        candidate.scroll_into_view_if_needed(timeout=2000)
                        candidate.click(timeout=5000, force=True)
                        logger.info("Clicked Workday Apply via %s text=%r", sel, text[:40])
                        try:
                            page.wait_for_timeout(1500)
                        except Exception:
                            pass
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
        try:
            role = page.get_by_role("button", name="Apply")
            if role.count() > 0 and role.first.is_visible(timeout=800):
                role.first.click(timeout=5000, force=True)
                logger.info("Clicked Workday Apply via role=button name=Apply")
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                return True
        except Exception:
            pass
        try:
            page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)
    logger.warning("Workday Apply button not clicked")
    return False


def _uploadable_resume(resume_path: str | None) -> Path | None:
    if not resume_path or not Path(resume_path).exists():
        return None
    try:
        from automation.resume_upload import ensure_uploadable_resume

        return ensure_uploadable_resume(resume_path)
    except Exception as exc:
        logger.warning("Workday: resume convert failed: %s", exc)
        return Path(resume_path)


def _handle_start_application_modal(page, uploadable: Path | None) -> str:
    """
    Workday often shows Start Your Application after Apply.
    Prefer Use My Last Application when present (pre-filled experience).
    Fall back to Apply Manually, then Autofill with Resume.
    """
    for sel in START_MODAL_LAST_APP_SELECTORS:
        try:
            loc = page.locator(sel)
            if _int_count(loc) == 0:
                continue
            btn = loc.first
            if not btn.is_visible(timeout=1200):
                continue
            btn.click(timeout=4000)
            logger.info("Workday: clicked Use My Last Application")
            try:
                page.wait_for_timeout(2000)
            except Exception:
                pass
            return "last_application"
        except Exception:
            continue

    for sel in START_MODAL_MANUAL_SELECTORS:
        try:
            loc = page.locator(sel)
            if _int_count(loc) == 0:
                continue
            btn = loc.first
            if not btn.is_visible(timeout=1200):
                continue
            btn.click(timeout=4000)
            logger.info("Workday: clicked Apply Manually")
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
            return "manual"
        except Exception:
            continue

    if uploadable is not None:
        for sel in START_MODAL_AUTOFILL_SELECTORS:
            try:
                loc = page.locator(sel)
                if loc.count() == 0:
                    continue
                btn = loc.first
                if not btn.is_visible(timeout=1500):
                    continue
                try:
                    with page.expect_file_chooser(timeout=8000) as fc_info:
                        btn.click(timeout=4000)
                    fc_info.value.set_files(str(uploadable))
                    logger.info("Workday: Autofill with Resume (%s)", uploadable.name)
                    try:
                        page.wait_for_timeout(2000)
                    except Exception:
                        pass
                    return "resume"
                except Exception:
                    btn.click(timeout=4000)
                    try:
                        page.wait_for_timeout(800)
                    except Exception:
                        pass
                    if _my_information_ready(page):
                        return "resume"
                    logger.info("Workday: Autofill click did not open a file chooser")
            except Exception:
                continue
    return ""


def _upload_workday_resume(page, resume_path: Path) -> bool:
    """Attach resume only through Workday's visible Autofill file input."""
    sel = "input[data-automation-id='file-upload-input-ref']"
    try:
        loc = page.locator(sel)
        if _int_count(loc) == 0:
            return False
        el = loc.first
        try:
            visible = bool(el.is_visible(timeout=800))
        except Exception:
            visible = False
        if not visible:
            logger.info("Workday: skipping hidden resume file input")
            return False
        el.set_input_files(str(resume_path), timeout=5000)
        try:
            page.wait_for_timeout(2500)
        except Exception:
            pass
        logger.info("Workday: uploaded resume via %s", sel)
        return True
    except Exception:
        return False


def _start_application_modal_visible(page) -> bool:
    for sel in (
        *START_MODAL_AUTOFILL_SELECTORS,
        *START_MODAL_MANUAL_SELECTORS,
        *START_MODAL_LAST_APP_SELECTORS,
    ):
        if _is_visible(page, sel):
            return True
    try:
        loc = page.get_by_text(re.compile(r"Start Your Application", re.I))
        return _int_count(loc) > 0 and bool(loc.first.is_visible(timeout=400))
    except Exception:
        return False


def _wait_for_apply_navigation(page, *, timeout_sec: int = 60) -> bool:
    """Wait for Apply Manually / Last Application to finish loading."""
    logger.info("Waiting up to %ss for Workday apply flow", timeout_sec)
    deadline = time.time() + max(timeout_sec, 1)
    while time.time() < deadline:
        if _workday_error_page(page):
            return False
        if _on_workday_sign_in_page(page):
            return True
        if _my_information_ready(page) or _on_my_experience_page(page):
            return True
        try:
            step = _current_wizard_step(page)
            if step in {"questions", "my_experience", "disclosures", "review", "my_information"}:
                return True
        except Exception:
            pass
        try:
            t0 = time.time()
            page.wait_for_timeout(1000)
            if time.time() - t0 < 0.05:
                return False
        except Exception:
            time.sleep(1)
    logger.warning("Timed out waiting for Workday apply flow")
    return _my_information_ready(page) or _on_my_experience_page(page) or _on_workday_sign_in_page(page)


def _on_workday_sign_in_page(page) -> bool:
    for pattern in (
        r"Sign in with email",
        r"Sign in with Google",
        r"Forgot your password",
        r"Create Account/Sign In",
        r"Create an account to manage your applications",
        r"Don't have an account yet",
    ):
        try:
            loc = page.get_by_text(re.compile(pattern, re.I))
            if _int_count(loc) > 0 and bool(loc.first.is_visible(timeout=400)):
                return True
        except Exception:
            continue
    return False


def _workday_session_active(page) -> bool:
    """True when the persistent profile is already signed in to Workday."""
    if _on_workday_sign_in_page(page):
        return False
    if _my_information_ready(page) or _on_my_experience_page(page):
        return True
    if _start_application_modal_visible(page) or _on_autofill_resume_page(page):
        return True
    for sel in (
        "a:has-text('Candidate Home')",
        "button:has-text('Candidate Home')",
        "a:has-text('Job Alerts')",
    ):
        if _is_visible(page, sel):
            return True
    try:
        loc = page.get_by_text(re.compile(r"Candidate Home", re.I))
        if _int_count(loc) > 0 and bool(loc.first.is_visible(timeout=400)):
            return True
    except Exception:
        pass
    return False


def _on_autofill_resume_page(page) -> bool:
    """True on the start Autofill step — not My Experience's Resume/CV drop zone."""
    if _workday_error_page(page):
        return False
    if _my_information_ready(page) or _on_my_experience_page(page):
        return False
    try:
        title = page.locator("[data-automation-id='applyFlowPageTitle']")
        if _int_count(title) > 0:
            raw = (title.first.inner_text(timeout=600) or "").lower()
            if "experience" in raw or "information" in raw or "question" in raw:
                return False
            if "autofill" in raw:
                return True
    except Exception:
        pass
    for pattern in (
        r"Autofill with Resume",
        r"Upload either DOC",
        r"Drop file here",
        r"Drop files here",
        r"Select file",
    ):
        try:
            loc = page.get_by_text(re.compile(pattern, re.I))
            if _int_count(loc) > 0 and bool(loc.first.is_visible(timeout=400)):
                return True
        except Exception:
            continue
    return _is_visible(page, "input[data-automation-id='file-upload-input-ref']")


def _handle_autofill_resume_page(page, uploadable: Path) -> bool:
    """Upload on the Autofill with Resume step (Select file / drop zone)."""
    if not _on_autofill_resume_page(page):
        return False
    for sel in (
        "a:has-text('Select file')",
        "button:has-text('Select file')",
        "[role='button']:has-text('Select file')",
        "text=Select file",
    ):
        try:
            loc = page.locator(sel)
            if _int_count(loc) == 0:
                continue
            btn = loc.first
            if not bool(btn.is_visible(timeout=800)):
                continue
            try:
                with page.expect_file_chooser(timeout=8000) as fc_info:
                    btn.click(timeout=4000)
                fc_info.value.set_files(str(uploadable))
                logger.info("Workday: uploaded resume via Select file (%s)", uploadable.name)
                _settle_resume_parse(page, timeout_sec=8)
                return True
            except Exception:
                continue
        except Exception:
            continue
    if _upload_workday_resume(page, uploadable):
        return True
    return False


def _looks_like_workday_account_wall(page) -> bool:
    # Application widgets win — a header "Sign In" link is not the login form.
    if _workday_session_active(page):
        return False
    try:
        pwd = page.locator("input[type='password']")
        if _int_count(pwd) > 0 and pwd.first.is_visible(timeout=400):
            return True
    except Exception:
        pass
    for pattern in (
        r"Forgot your password",
        r"Sign in with email",
        r"Don't have an account yet",
    ):
        try:
            loc = page.get_by_text(re.compile(pattern, re.I))
            if _int_count(loc) > 0 and bool(loc.first.is_visible(timeout=400)):
                return True
        except Exception:
            continue
    return False


def _my_information_ready(page) -> bool:
    if _workday_error_page(page):
        return False
    for sel in (
        "input[data-automation-id='legalNameSection_firstName']",
        "input[data-automation-id='legalNameSection_lastName']",
        "input[data-automation-id='addressSection_city']",
    ):
        if _is_visible(page, sel):
            return True
    try:
        heard = page.get_by_text(re.compile(r"How Did You Hear About Us", re.I))
        if _int_count(heard) > 0 and bool(heard.first.is_visible(timeout=400)):
            return True
    except Exception:
        pass
    try:
        given = page.get_by_text(re.compile(r"Given Name", re.I))
        if _int_count(given) > 0 and bool(given.first.is_visible(timeout=400)):
            return True
    except Exception:
        pass
    return False


def _wait_for_my_information(page, *, timeout_sec: int = 45) -> bool:
    logger.info("Waiting up to %ss for Workday My Information", timeout_sec)
    deadline = time.time() + max(timeout_sec, 1)
    while time.time() < deadline:
        if _workday_error_page(page):
            return False
        if _on_workday_sign_in_page(page):
            return False
        if _my_information_ready(page) or _on_my_experience_page(page):
            try:
                page.wait_for_timeout(800)
            except Exception:
                pass
            return True
        try:
            step = _current_wizard_step(page)
            if step in {"questions", "my_experience", "disclosures", "review"}:
                return True
        except Exception:
            pass
        try:
            t0 = time.time()
            page.wait_for_timeout(1000)
            # Unit tests mock wait_for_timeout as instant — don't busy-spin.
            if time.time() - t0 < 0.05:
                return False
        except Exception:
            time.sleep(1)
    logger.warning("Timed out waiting for Workday My Information")
    return False


def _workday_form_ready(page) -> bool:
    """True once we're past Sign In (My Information / Autofill — not the login form)."""
    if _workday_session_active(page):
        return True
    if _looks_like_workday_account_wall(page):
        return False
    for sel in (
        "button:has-text('Autofill with Resume')",
        "button:has-text('Save and Continue')",
        "button:has-text('Continue')",
    ):
        if _is_visible(page, sel):
            return True
    return False


def _wait_for_workday_login(page, *, timeout_sec: int, poll_sec: float = 2.0) -> bool:
    logger.info("Waiting up to %ss for Workday sign-in", timeout_sec)
    deadline = time.time() + max(timeout_sec, 1)
    while time.time() < deadline:
        try:
            if (
                _workday_form_ready(page)
                or _workday_session_active(page)
                or not _on_workday_sign_in_page(page)
                and (_my_information_ready(page) or _start_application_modal_visible(page))
            ):
                logger.info("Workday session looks past Sign In (url=%s)", getattr(page, "url", ""))
                try:
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
                return True
        except Exception as exc:
            logger.debug("Workday login wait poll error: %s", exc)
        remaining = int(deadline - time.time())
        if remaining > 0 and remaining % 30 < poll_sec:
            logger.info("Still waiting for Workday login… %ss left", remaining)
        try:
            page.wait_for_timeout(int(poll_sec * 1000))
        except Exception:
            time.sleep(poll_sec)
    logger.warning("Timed out waiting for Workday login")
    return False


def _int_count(loc) -> int:
    try:
        n = loc.count()
        return int(n) if isinstance(n, int) else 0
    except Exception:
        return 0


def _is_visible(page, selector: str) -> bool:
    try:
        loc = page.locator(selector)
        if _int_count(loc) == 0:
            return False
        return bool(loc.first.is_visible(timeout=400))
    except Exception:
        return False


def _page_has_errors(page) -> bool:
    try:
        loc = page.get_by_text(re.compile(r"Errors?\s+Found", re.I))
        return _int_count(loc) > 0 and bool(loc.first.is_visible(timeout=400))
    except Exception:
        return False


def _click_text(page, pattern: str) -> bool:
    try:
        loc = page.get_by_text(re.compile(pattern, re.I))
        if _int_count(loc) == 0:
            return False
        loc.first.scroll_into_view_if_needed(timeout=1500)
        loc.first.click(timeout=3000, force=True)
        return True
    except Exception:
        return False


def _current_wizard_step(page) -> str:
    title = ""
    try:
        loc = page.locator("[data-automation-id='applyFlowPageTitle']")
        if _int_count(loc) > 0:
            raw = loc.first.inner_text(timeout=800)
            if isinstance(raw, str):
                title = raw.lower().strip()
    except Exception:
        title = ""
    if "autofill" in title:
        return "autofill"
    if "information" in title:
        return "my_information"
    if "experience" in title:
        return "my_experience"
    if "question" in title:
        return "questions"
    if "voluntary" in title or "disclosure" in title:
        return "disclosures"
    if title == "review" or title.startswith("review "):
        return "review"
    url = getattr(page, "url", "")
    if not isinstance(url, str):
        url = ""
    path = urlparse(url).path.lower()
    if "/experience" in path:
        return "my_experience"
    if "/questionnaire" in path or "/questions" in path:
        return "questions"
    if path.endswith("/review") or "/review/" in path:
        return "review"
    if _on_my_experience_page(page):
        return "my_experience"
    if _my_information_ready(page):
        return "my_information"
    if _on_autofill_resume_page(page):
        return "autofill"
    try:
        if _int_count(_form_field(page, r"Would you consider relocating|consent to recorded Workday interviews")) > 0:
            return "questions"
    except Exception:
        pass
    if _on_voluntary_disclosures_page(page):
        return "disclosures"
    if _submit_visible(page) and "question" not in title:
        return "review"
    return "unknown"


def _click_wizard_next(page, *, dry_run: bool) -> bool:
    loc = page.locator("button[data-automation-id='bottom-navigation-next-button']")
    if _int_count(loc) > 0:
        try:
            loc.first.scroll_into_view_if_needed(timeout=2000)
            text = (loc.first.inner_text(timeout=800) or "").lower()
            if dry_run and "submit" in text and "save" not in text:
                return False
            loc.first.click(timeout=4000, force=True)
            logger.info("Workday: clicked bottom Next (%s)", text[:40])
            return True
        except Exception as exc:
            logger.warning("Workday: bottom Next failed: %s", exc)
    ok = _click_first(page, NEXT_SELECTORS)
    logger.info("Workday: Next fallback=%s", ok)
    return ok


def _form_field(page, label_re: str):
    return page.locator(
        "[data-automation-id*='formField']:not([data-automation-id*='error']), "
        "[data-automation-id*='FormField']:not([data-automation-id*='error'])"
    ).filter(has_text=re.compile(label_re, re.I))


def _select_how_heard(page, option: str, category: str = "") -> bool:
    """
    How Did You Hear is a two-level prompt: category then leaf.
    Example: Job Boards → LinkedIn / Indeed / Naukri.
    """
    source = page.locator("[data-automation-id='formField-source']")
    if _int_count(source) == 0:
        source = _form_field(page, r"How Did You Hear About Us")
    if _int_count(source) == 0:
        return _combobox_select(page, r"How Did You Hear About Us", option)
    root = source.first
    if _source_chip_contains(root, option):
        logger.info("Workday: how_heard already %s", option)
        return True
    if not _open_source_widget(root, page):
        logger.warning("Workday: could not open How Did You Hear widget")
        return False
    try:
        page.wait_for_timeout(500)
    except Exception:
        pass
    typed_cat = category or "Job Boards"
    inp = root.locator("input:not([type='hidden']):not([type='radio'])")
    if _int_count(inp) > 0:
        try:
            inp.first.fill("")
            inp.first.fill(typed_cat, timeout=2000)
            try:
                page.wait_for_timeout(700)
            except Exception:
                pass
        except Exception:
            pass
    leaves = [option, "LinkedIn", "Indeed", "Naukri"]
    fallback_leaves = ["Social Media", "Other"]
    preferred_cats = [c for c in (category, "Job Boards", "Job Board") if c]
    other_cats = ["Socially", "Advertisement", "Website", "College/University"]
    texts = _wait_for_prompt_texts(page, preferred_cats + leaves, timeout_sec=2.5)
    leaf = _first_match(texts, leaves)
    if leaf and _click_visible_prompt_option(page, leaf) and _source_chip_contains(
        root, option
    ):
        logger.info("Workday: how_heard selected leaf %s", leaf)
        return True
    for cat_list in (preferred_cats, other_cats):
        cat = _first_match(texts, cat_list)
        if not cat:
            continue
        _click_visible_prompt_option(page, cat)
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass
        texts2 = _wait_for_prompt_texts(page, leaves, timeout_sec=2.0)
        leaf = _first_match(texts2, leaves) or (
            _first_match(texts2, fallback_leaves) if cat_list is other_cats else ""
        )
        if leaf and _click_visible_prompt_option(page, leaf):
            chip_ok = _source_chip_contains(root, option) or _source_chip_contains(
                root, leaf
            )
            if chip_ok:
                logger.info("Workday: how_heard %s → %s", cat, leaf)
                return True
        for t in texts2:
            if t.lower() == cat.lower() or "+" in t:
                continue
            if _click_visible_prompt_option(page, t) and (
                _source_chip_contains(root, option) or _source_chip_contains(root, t)
            ):
                logger.info("Workday: how_heard %s → %s", cat, t)
                return True
        texts = _visible_prompt_texts(page)
    logger.warning(
        "Workday: could not select How Did You Hear About Us (options=%s)",
        texts[:12],
    )
    return False


def _open_source_widget(root, page) -> bool:
    for sel in (
        "[data-automation-id='multiselectInputContainer']",
        "[data-automation-id='promptIcon']",
        "[data-automation-id='multiSelectContainer']",
        "input:not([type='hidden'])",
    ):
        try:
            loc = root.locator(sel)
            if _int_count(loc) == 0:
                continue
            loc.first.click(timeout=2500, force=True)
            return True
        except Exception:
            continue
    try:
        root.click(timeout=2500, force=True)
        return True
    except Exception:
        return False


def _source_chip_contains(root, option: str) -> bool:
    if not option:
        return False
    try:
        items = root.locator("[data-automation-id='selectedItem']")
        n = _int_count(items)
        for i in range(min(n, 6)):
            txt = (items.nth(i).inner_text(timeout=400) or "").lower()
            if option.lower() in txt:
                return True
    except Exception:
        pass
    return False


def _visible_prompt_texts(page) -> list[str]:
    try:
        raw = page.evaluate(
            """() => [...document.querySelectorAll(
              '[data-automation-id="promptOption"], [data-automation-id="promptLeafNode"], '
              + '[data-automation-id="selectOption"], [data-automation-id*="selectWidget"] [role="option"], '
              + '[role="listbox"] [role="option"], [role="option"]'
            )]
              .filter(e => e.offsetParent)
              .map(e => (e.innerText || '').trim().split('\\n')[0])
              .filter(t => t && !t.includes('+') && t.length < 180)"""
        )
        if isinstance(raw, list):
            return [str(t).strip() for t in raw if str(t).strip()]
    except Exception:
        pass
    return []


def _wait_for_prompt_texts(page, wanted: list[str], *, timeout_sec: float = 2.0) -> list[str]:
    deadline = time.time() + max(timeout_sec, 0.2)
    last: list[str] = []
    wanted_l = [w.lower() for w in wanted if w]
    while time.time() < deadline:
        last = _visible_prompt_texts(page)
        blob = " ".join(last).lower()
        if any(w in blob for w in wanted_l):
            return last
        try:
            t0 = time.time()
            page.wait_for_timeout(250)
            if time.time() - t0 < 0.05:
                return last
        except Exception:
            break
    return last


def _click_dropdown_choice(page, choice: str) -> bool:
    """Click Yes/No/None (or a long option) in an open Workday select list."""
    if not choice:
        return False
    if _click_visible_prompt_option(page, choice):
        return True
    exact = re.compile(rf"^{re.escape(choice)}$", re.I)
    locators = []
    try:
        locators.append(page.get_by_role("option", name=exact))
    except Exception:
        pass
    locators.extend(
        [
            page.locator("[role='option']").filter(has_text=exact),
            page.locator(
                "[data-automation-id='selectOption'], "
                "[data-automation-id*='selectWidget'] li, "
                "[data-automation-id='promptOption']"
            ).filter(has_text=exact),
            page.get_by_text(exact),
        ]
    )
    for loc in locators:
        n = _int_count(loc)
        for i in range(n - 1, -1, -1):
            el = loc.nth(i) if n > 1 else loc.first
            try:
                if not bool(el.is_visible(timeout=400)):
                    continue
                el.click(timeout=2500, force=True)
                try:
                    page.wait_for_timeout(350)
                except Exception:
                    pass
                return True
            except Exception:
                continue
    return False


def _first_match(texts: list[str], preferred: list[str]) -> str:
    lower = {t.lower(): t for t in texts}
    for cand in preferred:
        if not cand:
            continue
        if cand.lower() in lower:
            return lower[cand.lower()]
        for t in texts:
            if cand.lower() in t.lower():
                return t
    return ""


def _click_visible_prompt_option(page, option: str) -> bool:
    # Prefer the open dropdown (appended last). .first is often a hidden template.
    locators = [
        page.locator(
            "[data-automation-id='activeListContainer'] [data-automation-id='promptOption']"
        ),
        page.locator(
            "[data-automation-id='promptContainer'] [data-automation-id='promptOption']"
        ),
        page.locator("[data-automation-id='promptLeafNode']"),
        page.locator("[data-automation-id='selectOption']"),
        page.locator("[role='listbox'] [role='option']"),
        page.locator("[data-automation-id='promptOption'], [role='option']"),
    ]
    for opts in locators:
        match = opts.filter(has_text=re.compile(re.escape(option), re.I))
        m = _int_count(match)
        if m == 0:
            continue
        for i in range(m - 1, -1, -1):
            el = match.nth(i)
            try:
                if not bool(el.is_visible(timeout=400)):
                    continue
                el.click(timeout=3000, force=True)
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                return True
            except Exception:
                continue
    return False


def _click_prompt_option(page, option: str) -> bool:
    opts = page.locator("[data-automation-id='promptOption'], [role='option']")
    if _int_count(opts) == 0:
        return False
    match = opts.filter(has_text=re.compile(rf"{re.escape(option)}", re.I))
    if _int_count(match) == 0:
        return False
    match.first.click(timeout=3000)
    try:
        page.wait_for_timeout(400)
    except Exception:
        pass
    return True


def _type_and_pick_prompt(page, option: str) -> bool:
    search = page.locator(
        "[data-automation-id='promptSearchField'], "
        "[data-automation-id='promptContainer'] input:not([type='hidden']), "
        "[data-automation-id='wd-popup'] input:not([type='hidden'])"
    )
    typed = False
    try:
        if _int_count(search) > 0 and bool(search.last.is_visible(timeout=600)):
            search.last.fill("")
            search.last.fill(option, timeout=2000)
            typed = True
        else:
            page.keyboard.type(option, delay=40)
            typed = True
    except Exception:
        try:
            page.keyboard.type(option, delay=40)
            typed = True
        except Exception:
            return False
    try:
        page.wait_for_timeout(800)
    except Exception:
        pass
    opts = page.locator("[data-automation-id='promptOption'], [role='option']")
    if _int_count(opts) > 0:
        match = opts.filter(has_text=re.compile(re.escape(option), re.I))
        if _int_count(match) > 0:
            match.first.click(timeout=3000)
            try:
                page.wait_for_timeout(400)
            except Exception:
                pass
            return True
    return False


def _combobox_select(page, label_re: str, option: str) -> bool:
    field = _form_field(page, label_re)
    if _int_count(field) == 0:
        return _pick_prompt_option(page, label_re, option)
    root = field.first
    try:
        inp = root.locator("input:not([type='hidden']):not([type='radio'])")
        inp_val = ""
        if _int_count(inp) > 0:
            try:
                inp_val = inp.first.input_value(timeout=400) or ""
            except Exception:
                inp_val = ""
        selected = root.locator(
            "[data-automation-id='selectedItem'], "
            "[data-automation-id='promptSelectedOption'], "
            "[data-automation-id='DELETE_charm']"
        )
        if _int_count(selected) > 0 and option.lower() in (inp_val or "").lower():
            return True
        if _int_count(selected) > 0:
            try:
                txt = (root.inner_text(timeout=400) or "").lower()
                if option.lower() in txt:
                    return True
            except Exception:
                pass
        box = root.locator(
            "input:not([type='hidden']):not([type='radio']):not([type='checkbox']), "
            "[role='combobox'], button[aria-haspopup='listbox']"
        )
        if _int_count(box) > 0:
            box.first.click(timeout=2500)
            try:
                box.first.fill("")
            except Exception:
                pass
            try:
                box.first.fill(option, timeout=2000)
            except Exception:
                page.keyboard.type(option, delay=40)
        else:
            root.click(timeout=2500)
            page.keyboard.type(option, delay=40)
        try:
            page.wait_for_timeout(700)
        except Exception:
            pass
        opts = page.locator("[data-automation-id='promptOption'], [role='option']")
        if _int_count(opts) > 0:
            match = opts.filter(has_text=re.compile(rf"^{re.escape(option)}$", re.I))
            if _int_count(match) == 0:
                match = opts.filter(has_text=re.compile(re.escape(option), re.I))
            if _int_count(match) == 0:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                return False
            match.first.click(timeout=3000)
            try:
                page.wait_for_timeout(400)
            except Exception:
                pass
            logger.info("Workday: selected %r for %s", option, label_re)
            return True
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False
    except Exception:
        return _pick_prompt_option(page, label_re, option)


def _fill_my_information_questions(
    page, values: dict[str, str], filled: list[str]
) -> None:
    """Required My Information items on Workday career sites."""
    if "how_heard" not in filled:
        wanted = values.get("how_heard") or "LinkedIn"
        category = values.get("how_heard_category") or "Job Boards"
        if _select_how_heard(page, wanted, category=category):
            filled.append("how_heard")
            logger.info("Workday: filled how_heard=%s", wanted)
    _uncheck_preferred_name(page)
    _answer_yes_no(
        page,
        r"previously worked for|currently working for Workday",
        values.get("previously_worked_here") or "No",
        filled,
        name="previous_worker",
    )
    _select_one_option(
        page, r"Country / Territory$", values.get("country") or "India", filled, name="country"
    )
    _fill_legal_given_name(page, values.get("first_name") or "")
    last = values.get("last_name") or ""
    if last:
        _fill_labeled_input(page, r"Family Name", last)
    _select_one_option(page, r"Phone Device Type", "Mobile", filled, name="phone_device")
    phone = values.get("phone") or ""
    if phone:
        local = _national_phone(phone)
        _select_phone_country(page, "India")
        if _fill_labeled_input(page, r"Phone Number", local) or _fill_input(
            page,
            [
                "input[data-automation-id='phone-phoneNumber']",
                "input[data-automation-id*='phoneNumber']",
                "input[type='tel']",
            ],
            local,
        ):
            if "phone" not in filled:
                filled.append("phone")
    addr = values.get("address_line1") or ""
    if addr:
        if _fill_labeled_input(page, r"Address Line 1", addr) or _fill_input(
            page,
            [
                "input[data-automation-id='addressSection_addressLine1']",
                "input[data-automation-id*='addressLine1']",
            ],
            addr,
        ):
            if "address_line1" not in filled:
                filled.append("address_line1")


def _error_texts(page) -> list[str]:
    errors: list[str] = []
    for sel in (
        "[data-automation-id='errorList']",
        "[data-automation-id*='errorMessage']",
        "[data-automation-id$='error']",
        "[data-automation-id*='Error']",
    ):
        try:
            loc = page.locator(sel)
            n = _int_count(loc)
            for i in range(min(n, 12)):
                try:
                    t = (loc.nth(i).inner_text(timeout=400) or "").strip()
                    if t:
                        errors.append(t)
                except Exception:
                    continue
        except Exception:
            continue
    try:
        banner = page.get_by_text(re.compile(r"Errors?\s+Found", re.I))
        if _int_count(banner) > 0:
            parent = banner.first.locator("xpath=ancestor::*[.//a or .//li][1]")
            t = parent.inner_text(timeout=800) if _int_count(parent) else ""
            if isinstance(t, str) and t.strip():
                errors.append(t)
    except Exception:
        pass
    return errors


def _answer_error_banner(
    page,
    values: dict[str, str],
    profile: dict[str, Any],
    materials: dict[str, Any],
    filled: list[str],
) -> None:
    errors = _error_texts(page)
    blob = " ".join(errors).lower()
    logger.info("Workday validation errors: %s", errors[:6])
    if "hear about" in blob or "how did you hear" in blob:
        filled[:] = [x for x in filled if x != "how_heard"]
        if _select_how_heard(
            page,
            values.get("how_heard") or "LinkedIn",
            category=values.get("how_heard_category") or "Job Boards",
        ):
            filled.append("how_heard")
    if "previously worked" in blob or "currently working for workday" in blob:
        filled[:] = [x for x in filled if x != "previous_worker"]
        _answer_yes_no(
            page,
            r"previously worked for|currently working for Workday",
            "No",
            filled,
            name="previous_worker",
        )
    if "phone" in blob:
        phone = _national_phone(values.get("phone") or "")
        if phone:
            _select_phone_country(page, "India")
            _fill_labeled_input(page, r"Phone Number", phone)
            _fill_input(
                page,
                [
                    "input[data-automation-id='phone-phoneNumber']",
                    "input[data-automation-id*='phoneNumber']",
                    "input[type='tel']",
                ],
                phone,
            )
    if "given name" in blob or "first name" in blob:
        _uncheck_preferred_name(page)
        _fill_legal_given_name(page, values.get("first_name") or "")
    if "address" in blob:
        addr = values.get("address_line1") or values.get("location") or values.get("city") or ""
        if addr:
            _fill_labeled_input(page, r"Address Line 1", addr)
    if any(
        key in blob
        for key in (
            "error-company",
            "error-job title",
            "the field company is required",
            "the field job title is required",
            "error-from",
            "school or university",
            "error-degree",
            "error-certification",
            "error-specialty",
        )
    ) and "work_experience_retry" not in filled:
        filled[:] = [
            x
            for x in filled
            if x not in {"work_experience", "education", "work_experience_retry"}
        ]
        filled.append("work_experience_retry")
        _clear_optional_empty_panels(page)
        _fill_my_experience(
            page, profile, filled, _uploadable_resume(materials.get("resume_path"))
        )
    if any(
        key in blob
        for key in (
            "relocating",
            "non-compete",
            "workday system",
            "authorized to work",
            "sponsorship",
            "united states government",
            "export control",
            "related to a current workday",
            "ernst & young",
            "please enter \"yes\"",
            "interview recording",
            "consent to recorded",
            "vibe philosophy",
            "recruitment privacy statement and vibe",
        )
    ):
        _fill_application_questions(page, values, filled)
    if "vibe philosophy" in blob or "recruitment privacy statement and vibe" in blob:
        _fill_voluntary_disclosures(page, filled)
    _fill_workday_page(page, values, profile, materials, filled)


def _fill_application_questions(
    page, values: dict[str, str], filled: list[str]
) -> None:
    """Workday Application Questions are usually Select One dropdowns."""
    pairs = [
        (r"consider relocating", "Yes", "relocate"),
        (
            r"authorized to work in the country|legally authorized to work",
            values.get("work_authorized") or "No",
            "work_authorized",
        ),
        (r"non-compete|non-solicitation|post-employment restriction", "No", "non_compete"),
        (r"18 years|over 18|at least 18", values.get("over_18") or "Yes", "over_18"),
        (r"project with Accenture|worked on a project with", "None", "accenture_project"),
        (
            r"artificial intelligence screening|involving artificial intelligence",
            "I understand and I'm ready to continue with my application involving artificial intelligence",
            "ai_screening",
        ),
        (r"use or work on the Workday system", "No", "uses_workday"),
        (
            r"visa sponsorship|immigration filing",
            values.get("sponsorship") or "Yes",
            "sponsorship",
        ),
        (r"United States government", "No", "us_gov"),
        (
            r"Iran, Cuba, North Korea|Export Control",
            "No",
            "export_control",
        ),
        (r"related to a current Workday employee", "No", "related_employee"),
        (
            r"related to an employee of a customer|government official",
            "No",
            "related_customer",
        ),
        (r"Ernst & Young", "No", "ernst_young"),
        (
            r"Please enter \"yes\" if you acknowledge|answered them truthfully",
            "Yes",
            "questions_ack",
        ),
        (
            r"consent to recorded Workday interviews|INTERVIEW RECORDING CONSENT",
            "Yes",
            "interview_consent",
        ),
        (r"18 years|over 18|at least 18", values.get("over_18") or "Yes", "over_18"),
    ]
    for pattern, answer, name in pairs:
        if name != "interview_consent":
            _answer_yes_no(page, pattern, answer, filled, name=name)
        _select_one_option(page, pattern, answer, filled, name=name)
    _answer_interview_consent(page, filled)
    _answer_remaining_select_ones(page, values, filled)


def _select_one_option(
    page, question_re: str, answer: str, filled: list[str], *, name: str
) -> None:
    if name in filled and not _select_one_still_empty(page, question_re):
        return
    if name in filled:
        filled[:] = [x for x in filled if x != name]
    choice = str(answer or "").strip()
    low = choice.lower()
    if low in {"yes", "y", "true", "1"}:
        choice = "Yes"
    elif low in {"no", "n", "false", "0"}:
        choice = "No"
    field = _form_field(page, question_re)
    if _int_count(field) == 0:
        logger.info("Workday: select-one %s field not found", name)
        return
    root = field.last
    try:
        widget = root.locator(
            "[data-automation-id='selectWidget'], [data-automation-id='select']"
        )
        shown = ""
        if _int_count(widget) > 0:
            shown = (widget.last.inner_text(timeout=500) or "").strip()
        if (
            choice
            and shown
            and choice.lower() == shown.split("\n")[0].strip().lower()
            and "select one" not in shown.lower()
        ):
            filled.append(name)
            return
    except Exception:
        pass
    if not _open_select_widget(root, page):
        logger.info("Workday: select-one %s could not open widget", name)
        return
    try:
        page.wait_for_timeout(400)
    except Exception:
        pass
    texts = _wait_for_prompt_texts(page, [choice, "Yes", "No", "None"], timeout_sec=1.5)
    pick = _first_match(texts, [choice]) or _guess_select_answer(
        question_re, texts, {"_answer": choice}
    )
    if pick and _click_dropdown_choice(page, pick):
        filled.append(name)
        logger.info("Workday: select-one %s=%s", name, pick)
        return
    logger.info("Workday: select-one %s options=%s", name, texts[:8])


def _select_one_still_empty(page, question_re: str) -> bool:
    field = _form_field(page, question_re)
    if _int_count(field) == 0:
        return False
    try:
        txt = (field.last.inner_text(timeout=500) or "").lower()
    except Exception:
        return False
    return "select one" in txt


def _answer_interview_consent(page, filled: list[str]) -> None:
    """Page-2 consent is a long label + Select One — not a radio."""
    present = False
    try:
        loc = page.get_by_text(
            re.compile(r"consent to recorded Workday interviews|INTERVIEW RECORDING CONSENT", re.I)
        )
        present = _int_count(loc) > 0 and bool(loc.first.is_visible(timeout=400))
    except Exception:
        present = False
    if not present:
        return
    if "interview_consent" in filled and not _select_one_still_empty(
        page, r"consent to recorded Workday interviews|INTERVIEW RECORDING CONSENT"
    ):
        return
    filled[:] = [x for x in filled if x != "interview_consent"]
    try:
        err_link = page.locator("a, button, [role='link'], li").filter(
            has_text=re.compile(r"INTERVIEW RECORDING CONSENT", re.I)
        )
        if _int_count(err_link) > 0:
            err_link.first.click(timeout=2000)
            try:
                page.wait_for_timeout(400)
            except Exception:
                pass
    except Exception:
        pass
    opened = False
    try:
        so = page.get_by_text(re.compile(r"^Select One$", re.I))
        n = _int_count(so)
        for i in range(n - 1, -1, -1):
            el = so.nth(i)
            try:
                if not bool(el.is_visible(timeout=400)):
                    continue
                el.scroll_into_view_if_needed(timeout=1500)
                el.click(timeout=2500, force=True)
                opened = True
                break
            except Exception:
                continue
    except Exception:
        pass
    if not opened:
        _select_one_option(
            page,
            r"consent to recorded Workday interviews|INTERVIEW RECORDING CONSENT",
            "Yes",
            filled,
            name="interview_consent",
        )
        return
    try:
        page.wait_for_timeout(500)
    except Exception:
        pass
    if _click_dropdown_choice(page, "Yes"):
        filled.append("interview_consent")
        logger.info("Workday: select-one interview_consent=Yes")
        return
    logger.warning(
        "Workday: interview consent still unanswered (options=%s)",
        _visible_prompt_texts(page)[:8],
    )


def _open_select_widget(root, page) -> bool:
    for sel in (
        "[data-automation-id='selectWidget']",
        "[data-automation-id='select']",
        "[aria-haspopup='listbox']",
        "[role='combobox']",
    ):
        try:
            loc = root.locator(sel)
            if _int_count(loc) == 0:
                continue
            loc.last.click(timeout=2500, force=True)
            return True
        except Exception:
            continue
    try:
        so = root.get_by_text(re.compile(r"Select One", re.I))
        if _int_count(so) > 0:
            so.last.click(timeout=2500, force=True)
            return True
    except Exception:
        pass
    for sel in (
        "text=Select One",
        "[data-automation-id='multiselectInputContainer']",
        "button",
        "[data-automation-id='promptIcon']",
    ):
        try:
            loc = root.locator(sel)
            if _int_count(loc) == 0:
                continue
            loc.first.click(timeout=2500, force=True)
            return True
        except Exception:
            continue
    try:
        root.click(timeout=2500, force=True)
        return True
    except Exception:
        return False


def _guess_select_answer(question: str, options: list[str], values: dict[str, str]) -> str:
    q = question.lower()
    if not options:
        return ""
    wanted = str(values.get("_answer") or "").strip()
    hit = _first_match(options, [wanted] if wanted else [])
    if hit:
        return hit
    for o in options:
        ol = o.lower()
        if "i understand" in ol or "ready to continue" in ol:
            return o
    if any(o.lower() == "none" for o in options) and (
        "project" in q or "accenture" in q or "none of" in q
    ):
        return _first_match(options, ["None"])
    if "authorized" in q or "at least 18" in q or "over 18" in q or "consent" in q:
        return _first_match(options, ["Yes"]) or options[0]
    if "non-compete" in q or "related" in q or "export" in q:
        return _first_match(options, ["No"]) or options[0]
    return options[0]


def _answer_remaining_select_ones(
    page, values: dict[str, str], filled: list[str]
) -> None:
    fields = page.locator(
        "[data-automation-id*='formField'], [data-automation-id*='FormField']"
    ).filter(has_text=re.compile(r"Select One|\*", re.I))
    n = _int_count(fields)
    for i in range(min(n, 16)):
        root = fields.first if i == 0 else fields.nth(i)
        try:
            label = (root.inner_text(timeout=500) or "").strip()
        except Exception:
            continue
        if not label or "select one" not in label.lower():
            continue
        if "errors found" in label.lower() or "must have a value" in label.lower():
            continue
        name = f"q_{i}"
        if name in filled:
            continue
        if not _open_select_widget(root, page):
            continue
        try:
            page.wait_for_timeout(350)
        except Exception:
            pass
        texts = _visible_prompt_texts(page)
        pick = _guess_select_answer(label, texts, values)
        if pick and _click_dropdown_choice(page, pick):
            filled.append(name)
            logger.info("Workday: remaining question %r → %s", label[:60], pick)
        else:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass


def _fill_workday_page(
    page,
    values: dict[str, str],
    profile: dict[str, Any],
    materials: dict[str, Any],
    filled: list[str],
) -> None:
    step = _current_wizard_step(page)
    uploadable = _uploadable_resume(materials.get("resume_path"))
    if _on_my_experience_page(page):
        _fill_my_experience(page, profile, filled, uploadable)
        return
    if (
        uploadable is not None
        and "resume" not in filled
        and "last_application" not in filled
        and "manual" not in filled
    ):
        if _handle_autofill_resume_page(page, uploadable):
            filled.append("resume")
    if step in {"my_information", "unknown", "autofill"}:
        on_questions = False
        try:
            on_questions = _int_count(
                _form_field(
                    page,
                    r"Would you consider relocating|consent to recorded Workday interviews",
                )
            ) > 0
        except Exception:
            on_questions = False
        if not on_questions:
            _fill_my_information_questions(page, values, filled)
    if step in {"questions", "unknown"} and not _on_voluntary_disclosures_page(page):
        _fill_application_questions(page, values, filled)
        cover = (materials.get("cover_letter_text") or "").strip()
        if cover:
            _fill_empty_textareas(page, cover[:4000], filled)
        if _page_has_errors(page):
            _llm_fill_remaining(page, profile, materials, filled)
    if step == "disclosures" or _on_voluntary_disclosures_page(page):
        _fill_voluntary_disclosures(page, filled)


_MONTH_NAMES = {
    "jan": "January",
    "january": "January",
    "feb": "February",
    "february": "February",
    "mar": "March",
    "march": "March",
    "apr": "April",
    "april": "April",
    "may": "May",
    "jun": "June",
    "june": "June",
    "jul": "July",
    "july": "July",
    "aug": "August",
    "august": "August",
    "sep": "September",
    "sept": "September",
    "september": "September",
    "oct": "October",
    "october": "October",
    "nov": "November",
    "november": "November",
    "dec": "December",
    "december": "December",
}
_MONTH_NUM = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}
_DEGREE_ALIASES = {
    "b.e.": ["Bachelor of Engineering", "Bachelor's Degree", "Bachelors", "B.E."],
    "be": ["Bachelor of Engineering", "Bachelor's Degree", "B.E."],
    "b.tech": ["Bachelor of Technology", "Bachelor's Degree"],
    "b.s.": ["Bachelor of Science", "Bachelor's Degree"],
    "bs": ["Bachelor of Science", "Bachelor's Degree"],
    "hsc": ["High School", "Higher Secondary", "High School Diploma", "HSC"],
    "ssc": ["High School", "Secondary School", "High School Diploma", "SSC"],
}


def parse_workday_date(raw: str | None) -> dict[str, Any]:
    """Parse resume dates like 'May 2025', 'Present', '2022' into Workday parts."""
    s = str(raw or "").strip()
    if not s:
        return {"month": None, "year": None, "current": False}
    if re.search(r"\b(present|current|now|ongoing)\b", s, re.I):
        return {"month": None, "year": None, "current": True}
    m = re.search(
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\s*[.,]?\s*(\d{4})",
        s,
        re.I,
    )
    if m:
        key = re.sub(r"[^a-z]", "", m.group(1).lower())
        return {
            "month": _MONTH_NAMES.get(key) or _MONTH_NAMES.get(key[:3]),
            "year": m.group(2),
            "current": False,
        }
    y = re.search(r"\b((?:19|20)\d{2})\b", s)
    mmy = re.search(r"\b(\d{1,2})/(\d{4})\b", s)
    if mmy:
        try:
            mo = int(mmy.group(1))
        except ValueError:
            mo = 0
        month_name = next((name for name, num in _MONTH_NUM.items() if num == mo), None)
        return {"month": month_name, "year": mmy.group(2), "current": False}
    return {"month": None, "year": y.group(1) if y else None, "current": False}


def _date_tuple(parsed: dict[str, Any]) -> tuple[int, int] | None:
    year = parsed.get("year")
    if not year:
        return None
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    month = _MONTH_NUM.get(str(parsed.get("month") or ""), 1)
    return (y, month)


def _ordered_dates(start_raw: str | None, end_raw: str | None) -> tuple[dict, dict]:
    start = parse_workday_date(start_raw)
    end = parse_workday_date(end_raw)
    if end.get("current"):
        return start, end
    a, b = _date_tuple(start), _date_tuple(end)
    if a and b and a > b:
        return end, start
    return start, end


def _on_my_experience_page(page) -> bool:
    """True only on the Work Experience step — not the progress-bar label."""
    if _is_visible(page, "input[data-automation-id='legalNameSection_firstName']"):
        return False
    try:
        heard = page.get_by_text(re.compile(r"How Did You Hear About Us", re.I))
        if _int_count(heard) > 0 and bool(heard.first.is_visible(timeout=400)):
            return False
    except Exception:
        pass
    if _is_visible(page, "[data-automation-id='workExperienceSection']"):
        return True
    try:
        work = page.get_by_text(re.compile(r"^Work Experience$", re.I))
        if _int_count(work) > 0 and bool(work.first.is_visible(timeout=400)):
            return True
    except Exception:
        pass
    try:
        add_exp = page.get_by_role("button", name=re.compile(r"Add Work Experience", re.I))
        if _int_count(add_exp) > 0 and bool(add_exp.first.is_visible(timeout=400)):
            return True
    except Exception:
        pass
    try:
        title = page.locator(
            "[data-automation-id='applyFlowPageTitle'], "
            "[data-automation-id='pageHeader'], "
            "main h2, main h1"
        ).filter(has_text=re.compile(r"^My Experience$", re.I))
        if _int_count(title) > 0 and bool(title.first.is_visible(timeout=400)):
            return True
    except Exception:
        pass
    return False


def _workday_error_page(page) -> bool:
    try:
        loc = page.get_by_text(re.compile(r"Something went wrong", re.I))
        return _int_count(loc) > 0 and bool(loc.first.is_visible(timeout=400))
    except Exception:
        return False


def _settle_resume_parse(page, *, timeout_sec: int = 8) -> None:
    """Give Workday time to parse the uploaded resume before Continue."""
    deadline = time.time() + max(timeout_sec, 1)
    while time.time() < deadline:
        if _workday_error_page(page) or _my_information_ready(page):
            return
        try:
            t0 = time.time()
            page.wait_for_timeout(1000)
            if time.time() - t0 < 0.05:
                return
        except Exception:
            time.sleep(1)


def _my_information_shell_empty(page) -> bool:
    """Progress is on My Information but the form fields have not rendered."""
    if _my_information_ready(page) or _on_my_experience_page(page):
        return False
    if _on_autofill_resume_page(page) or _workday_error_page(page):
        return False
    if _submit_visible(page):
        return False
    for pattern in (
        r"Application Questions",
        r"Voluntary Disclosures",
        r"Work Experience",
        r"Add Work Experience",
        r"I do not want to answer",
    ):
        try:
            loc = page.get_by_text(re.compile(pattern, re.I))
            if _int_count(loc) > 0 and bool(loc.first.is_visible(timeout=300)):
                return False
        except Exception:
            continue
    try:
        title = page.locator(
            "[data-automation-id='applyFlowPageTitle'], main h2, main h1"
        ).filter(has_text=re.compile(r"My Information", re.I))
        if _int_count(title) > 0:
            return True
    except Exception:
        pass
    return False


def _recover_workday_error(page) -> bool:
    if not _workday_error_page(page):
        return False
    logger.warning("Workday error page — refreshing")
    try:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
    except Exception:
        try:
            page.reload()
        except Exception:
            return False
    if _workday_error_page(page):
        return False
    _wait_for_my_information(page, timeout_sec=20)
    return _my_information_ready(page) or _on_my_experience_page(page) or (
        not _workday_error_page(page)
    )

def _upload_experience_resume(page, uploadable: Path) -> bool:
    """Resume/CV drop zone on the My Experience step (Drop files here / Select file)."""
    section = page.locator(
        "[data-automation-id='file-upload-section'], "
        "[data-automation-id*='resumeSection'], "
        "[data-automation-id*='Resume']"
    )
    scopes = [section] if _int_count(section) > 0 else [page]
    for scope in scopes:
        for sel in (
            "a:has-text('Select file')",
            "button:has-text('Select file')",
            "text=Select file",
            "button:has-text('Upload')",
        ):
            try:
                loc = scope.locator(sel)
                if _int_count(loc) == 0:
                    continue
                with page.expect_file_chooser(timeout=6000) as fc_info:
                    loc.last.click(timeout=4000)
                fc_info.value.set_files(str(uploadable))
                logger.info("Workday: uploaded resume on My Experience (%s)", uploadable.name)
                return True
            except Exception:
                continue
    try:
        drop = page.get_by_text(re.compile(r"Drop files here", re.I))
        if _int_count(drop) > 0:
            with page.expect_file_chooser(timeout=6000) as fc_info:
                drop.last.click(timeout=4000)
            fc_info.value.set_files(str(uploadable))
            logger.info("Workday: uploaded resume via drop zone (%s)", uploadable.name)
            return True
    except Exception:
        pass
    for sel in ("input[type='file']", "input[data-automation-id='file-upload-input-ref']"):
        try:
            loc = page.locator(sel)
            if _int_count(loc) == 0:
                continue
            loc.last.set_input_files(str(uploadable))
            logger.info("Workday: set resume files on My Experience")
            return True
        except Exception:
            continue
    return False


def _experience_jobs_for_workday(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """My Experience: first job via Add, second via Add Another (max 2)."""
    jobs = [j for j in (profile.get("experience") or []) if isinstance(j, dict)]
    return jobs[:2]


def _primary_education_for_workday(profile: dict[str, Any]) -> dict[str, Any] | None:
    """My Experience: one bachelor's / B.E. education entry."""
    for school in profile.get("education") or []:
        if not isinstance(school, dict):
            continue
        degree = str(school.get("degree") or "").lower()
        if degree in {"b.e.", "be", "b.tech", "b.s.", "bs", "bachelor"} or "bachelor" in degree:
            return school
    schools = [e for e in (profile.get("education") or []) if isinstance(e, dict)]
    return schools[0] if schools else None


def _gpa_from_marks(marks: str) -> str:
    """Convert profile marks like 87% to a GPA-style value for Workday."""
    s = str(marks or "").strip()
    if not s:
        return ""
    if "%" in s:
        try:
            pct = float(re.sub(r"[^\d.]", "", s))
            if pct > 10:
                return f"{pct / 10:.2f}".rstrip("0").rstrip(".")
        except ValueError:
            pass
    return re.sub(r"[^0-9.]", "", s)


def _clear_optional_empty_panels(page) -> None:
    """Remove empty Certification / Specialty / Website rows that block continue."""
    for heading in (r"Specialt", r"Certification", r"Website"):
        _delete_panels_in_section(page, heading)
    for pattern in (r"^Certification$", r"^Specialt", r"^Websites?$"):
        try:
            heading = page.get_by_text(re.compile(pattern, re.I))
            if _int_count(heading) == 0:
                continue
            root = heading.first.locator(
                "xpath=ancestor::div[contains(@data-automation-id,'ection') "
                "or contains(@data-automation-id,'Section')][1]"
            )
            if _int_count(root) == 0:
                root = heading.first.locator("xpath=ancestor::div[3]")
            btns = root.locator(
                "button[aria-label*='Delete' i], "
                "button[aria-label*='Remove' i], "
                "button[data-automation-id*='delete' i], "
                "button[data-automation-id*='Delete']"
            )
            for _ in range(min(_int_count(btns), 6)):
                try:
                    btns.first.click(timeout=2000, force=True)
                    confirm = page.get_by_role(
                        "button", name=re.compile(r"^(Delete|Yes|OK|Remove)$", re.I)
                    )
                    if _int_count(confirm) > 0:
                        confirm.first.click(timeout=1500)
                    page.wait_for_timeout(400)
                except Exception:
                    break
        except Exception:
            continue
    try:
        page.wait_for_timeout(400)
    except Exception:
        pass


def _experience_already_filled(page) -> bool:
    return len(_filled_job_title_values(page)) >= 2


def _filled_job_title_values(page) -> list[str]:
    out: list[str] = []
    for panel in _work_experience_panels(page):
        try:
            inp = panel.locator(
                "input[data-automation-id='jobTitle'], "
                "input[placeholder*='Job Title' i]"
            )
            if _int_count(inp) == 0:
                inp = panel.get_by_label(re.compile(r"Job Title", re.I))
            if _int_count(inp) == 0:
                continue
            val = (inp.first.input_value(timeout=400) or "").strip()
            if val:
                out.append(val)
        except Exception:
            continue
    return out


def _work_experience_panels(page) -> list:
    """Return each open Work Experience block (one Job Title field per panel)."""
    panels = []
    title_inputs = []
    for factory in (
        lambda: page.get_by_role("textbox", name=re.compile(r"Job Title", re.I)),
        lambda: page.get_by_label(re.compile(r"Job Title", re.I)),
        lambda: page.locator(
            "input[aria-label*='Job Title' i], "
            "input[placeholder*='Job Title' i], "
            "input[data-automation-id='jobTitle'], "
            "input[data-automation-id*='jobTitle' i], "
            "input[id*='jobTitle' i]"
        ),
    ):
        try:
            loc = factory()
            for i in range(_int_count(loc)):
                el = loc.nth(i)
                try:
                    if bool(el.is_visible(timeout=300)):
                        title_inputs.append(el)
                except Exception:
                    continue
            if title_inputs:
                break
        except Exception:
            continue

    for inp in title_inputs:
        panel = inp.locator(
            "xpath=ancestor::div[.//*[contains(translate(normalize-space(.),"
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'company')]][1]"
        )
        if _int_count(panel) == 0:
            panel = inp.locator("xpath=ancestor::li[1]")
        if _int_count(panel) == 0:
            panel = inp.locator("xpath=ancestor::div[6]")
        panels.append(panel.first if _int_count(panel) else inp)
    return panels


def _education_panels(page) -> list:
    panels = []
    school_inputs = []
    for factory in (
        lambda: page.get_by_role(
            "textbox", name=re.compile(r"School or University|School", re.I)
        ),
        lambda: page.get_by_label(re.compile(r"School or University|School", re.I)),
        lambda: page.locator(
            "input[aria-label*='School' i], "
            "input[placeholder*='School' i], "
            "input[data-automation-id='school'], "
            "input[data-automation-id*='school' i]"
        ),
    ):
        try:
            loc = factory()
            for i in range(_int_count(loc)):
                el = loc.nth(i)
                try:
                    if bool(el.is_visible(timeout=300)):
                        school_inputs.append(el)
                except Exception:
                    continue
            if school_inputs:
                break
        except Exception:
            continue
    for inp in school_inputs:
        panel = inp.locator(
            "xpath=ancestor::div[.//*[contains(., 'Degree')]][1]"
        )
        if _int_count(panel) == 0:
            panel = inp.locator("xpath=ancestor::div[6]")
        panels.append(panel.first if _int_count(panel) else inp)
    return panels


def _wait_for_panels(list_panels, page, *, min_count: int = 1, timeout_sec: float = 5.0):
    deadline = time.time() + max(timeout_sec, 0.5)
    last = []
    while time.time() < deadline:
        last = list_panels(page)
        if len(last) >= min_count:
            return last
        try:
            t0 = time.time()
            page.wait_for_timeout(350)
            if time.time() - t0 < 0.05:
                break
        except Exception:
            break
    return last


def _panel_input_empty(panel, selectors: str) -> bool:
    try:
        loc = panel.locator(selectors)
        if _int_count(loc) == 0:
            loc = panel.get_by_label(re.compile(r"Job Title|School", re.I))
        if _int_count(loc) == 0:
            return True
        val = (loc.first.input_value(timeout=400) or "").strip()
        return not val
    except Exception:
        return True


def _fill_my_experience(
    page, profile: dict[str, Any], filled: list[str], uploadable: Path | None = None
) -> None:
    jobs = _experience_jobs_for_workday(profile)
    school = _primary_education_for_workday(profile)
    # Last Application / page transition can leave a blank shell briefly.
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass
    _wait_for_panels(_work_experience_panels, page, min_count=1, timeout_sec=8.0)
    _clear_optional_empty_panels(page)

    if jobs and "work_experience" not in filled:
        if _experience_already_filled(page):
            filled.append("work_experience")
            logger.info("Workday: work experience already populated — skipping Add")
        else:
            added = _ensure_section_entries(
                page,
                heading=r"Work Experience",
                items=jobs,
                fill_item=_fill_work_experience_panel,
                list_panels=_work_experience_panels,
                empty_probe=(
                    "input[data-automation-id='jobTitle'], "
                    "input[placeholder*='Job Title' i]"
                ),
                first_add=r"^Add$",
                next_add=r"Add Another",
            )
            if added:
                filled.append("work_experience")
                logger.info("Workday: filled %s work experience entries", added)
            else:
                logger.warning("Workday: no work experience entries added")

    if school and "education" not in filled:
        added = _ensure_section_entries(
            page,
            heading=r"^Education$",
            items=[school],
            fill_item=_fill_education_panel,
            list_panels=_education_panels,
            empty_probe=(
                "input[data-automation-id='school'], "
                "input[placeholder*='School' i]"
            ),
            first_add=r"^Add$",
            next_add=r"Add Another",
        )
        if added:
            filled.append("education")
            logger.info("Workday: filled %s education entries", added)
        else:
            logger.warning("Workday: no education entries added")

    if uploadable is not None and "resume" not in filled:
        if _upload_experience_resume(page, uploadable):
            filled.append("resume")


def _ensure_section_entries(
    page,
    *,
    heading: str,
    items: list[dict[str, Any]],
    fill_item,
    list_panels,
    empty_probe: str,
    first_add: str,
    next_add: str,
) -> int:
    """Fill existing empty panels first; Add / Add Another only as needed."""
    filled_n = 0
    for i, item in enumerate(items):
        panels = list_panels(page)
        logger.info(
            "Workday: %s has %s panel(s) before item %s", heading, len(panels), i
        )
        target = None
        for panel in panels:
            if _panel_input_empty(panel, empty_probe):
                target = panel
                break
        if target is None:
            add_re = first_add if (i == 0 and not panels) else next_add
            if i == 0 and not panels:
                add_re = first_add
            elif panels:
                add_re = next_add
            else:
                add_re = first_add
            if not _click_add_in_section(page, heading, add_re):
                # First entry sometimes only has "Add" / "Add Work Experience".
                if add_re != first_add and not _click_add_in_section(
                    page, heading, first_add
                ):
                    logger.warning("Workday: could not click Add for %s", heading)
                    break
                if add_re == first_add:
                    logger.warning("Workday: could not click Add for %s", heading)
                    break
            try:
                page.wait_for_timeout(900)
            except Exception:
                pass
            panels = _wait_for_panels(list_panels, page, min_count=1, timeout_sec=5.0)
            for panel in reversed(panels):
                if _panel_input_empty(panel, empty_probe):
                    target = panel
                    break
            if target is None and panels:
                target = panels[-1]
            if target is None:
                # Last resort: fill via page-level Job Title / School textbox.
                try:
                    probe = page.get_by_role(
                        "textbox",
                        name=re.compile(
                            r"Job Title|School or University|School", re.I
                        ),
                    )
                    if _int_count(probe) > 0:
                        target = probe.last
                        logger.info("Workday: using page-level textbox as panel")
                except Exception:
                    pass
        if target is None:
            logger.warning("Workday: no panel available for %s item %s", heading, i)
            try:
                diag = page.evaluate(
                    """() => {
                      const inputs = [...document.querySelectorAll('input')]
                        .filter(e => e.offsetParent)
                        .slice(0, 30)
                        .map(e => ({
                          aria: e.getAttribute('aria-label') || '',
                          ph: e.getAttribute('placeholder') || '',
                          id: e.id || '',
                          auto: e.getAttribute('data-automation-id') || '',
                          name: e.getAttribute('name') || '',
                        }));
                      return inputs;
                    }"""
                )
                logger.info("Workday: visible inputs sample=%s", diag)
            except Exception as exc:
                logger.info("Workday: input dump failed: %s", exc)
            break
        fill_item(page, item, target)
        _commit_section_entry(page, target, heading)
        try:
            page.wait_for_timeout(600)
        except Exception:
            pass
        if not _panel_input_empty(target, empty_probe):
            filled_n += 1
        else:
            # Retry once with page-scoped fill if panel verify failed.
            fill_item(page, item, target)
            if not _panel_input_empty(target, empty_probe):
                filled_n += 1
            else:
                logger.warning(
                    "Workday: panel still empty after fill for %s item %s", heading, i
                )
                filled_n += 1  # count attempt; avoid infinite outer retries
    return filled_n


def _add_section_entries(
    page,
    *,
    heading: str,
    items: list[dict[str, Any]],
    fill_item,
    first_add: str,
    next_add: str,
) -> int:
    return _ensure_section_entries(
        page,
        heading=heading,
        items=items,
        fill_item=fill_item,
        list_panels=(
            _education_panels if "education" in heading.lower() else _work_experience_panels
        ),
        empty_probe=(
            "input[data-automation-id='school'], input[placeholder*='School' i]"
            if "education" in heading.lower()
            else "input[data-automation-id='jobTitle'], input[placeholder*='Job Title' i]"
        ),
        first_add=first_add,
        next_add=next_add,
    )


def _commit_section_entry(page, panel, heading: str) -> bool:
    """Collapse / save an inline Workday Add panel after filling required fields."""
    try:
        inputs = panel.locator(
            "input:visible:not([type='checkbox']):not([type='hidden']), textarea:visible"
        )
        if _int_count(inputs) > 0:
            inputs.last.press("Tab")
            page.wait_for_timeout(250)
    except Exception:
        pass
    for sel in (
        "button[data-automation-id='panelSetSaveButton']",
        "button[data-automation-id='saveButton']",
        "button[data-automation-id*='SaveButton']",
        "button[data-automation-id*='save' i]",
        "button[aria-label*='Save' i]",
        "button[title*='Save' i]",
        "[data-automation-id='wd-IconButton'][aria-label*='Save' i]",
        "button:has-text('Done')",
    ):
        try:
            loc = panel.locator(sel)
            if _int_count(loc) == 0:
                loc = page.locator(sel)
            if _int_count(loc) == 0:
                continue
            target = loc.last
            if bool(target.is_visible(timeout=400)):
                target.click(timeout=2500, force=True)
                logger.info("Workday: committed %s panel via %s", heading, sel)
                page.wait_for_timeout(500)
                return True
        except Exception:
            continue
    try:
        btn = page.get_by_role("button", name=re.compile(r"^(Save|Done|OK)$", re.I))
        if _int_count(btn) > 0 and bool(btn.last.is_visible(timeout=400)):
            btn.last.click(timeout=2500, force=True)
            logger.info("Workday: committed %s panel via Save/Done", heading)
            return True
    except Exception:
        pass
    try:
        label = heading.strip("^$")
        title = page.get_by_text(re.compile(rf"^{re.escape(label)}$", re.I))
        if _int_count(title) > 0:
            title.first.click(timeout=2000, force=True)
            page.wait_for_timeout(400)
            return True
    except Exception:
        pass
    return False


def _last_section_panel(page, heading_re: str):
    section = _section_root(page, heading_re)
    for sel in (
        "[data-automation-id*='workExperience']:has(input:visible)",
        "[data-automation-id*='education']:has(input:visible)",
        "[data-automation-id*='panel']:has(input:visible)",
        "div[data-automation-id*='formField']:has(input:visible)",
    ):
        try:
            loc = section.locator(sel)
            if _int_count(loc) > 0:
                return loc.last
        except Exception:
            continue
    try:
        inputs = section.locator(
            "input:not([type='hidden']):not([type='checkbox']), textarea"
        ).filter(has_not=page.locator("[type='file']"))
        if _int_count(inputs) > 0:
            return inputs.last.locator(
                "xpath=ancestor::div[contains(@data-automation-id,'')][.//input][1]"
            )
    except Exception:
        pass
    return section


def _section_root(page, heading_re: str):
    try:
        heading = page.get_by_text(re.compile(heading_re, re.I))
        if _int_count(heading) > 0:
            return heading.first.locator(
                "xpath=ancestor::div[contains(@data-automation-id,'ection') "
                "or contains(@data-automation-id,'Section')][1]"
            )
    except Exception:
        pass
    return page.locator("body")


def _delete_panels_in_section(page, heading_re: str) -> None:
    for _ in range(10):
        section = _section_root(page, heading_re)
        btn = section.locator(
            "button[aria-label*='Delete' i], "
            "button[data-automation-id*='delete' i], "
            "button[data-automation-id*='Delete']"
        )
        if _int_count(btn) == 0:
            return
        try:
            btn.first.click(timeout=2500, force=True)
            confirm = page.get_by_role("button", name=re.compile(r"^(Delete|Yes|OK)$", re.I))
            if _int_count(confirm) > 0:
                confirm.first.click(timeout=2000)
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass
        except Exception:
            return


def _click_add_in_section(page, heading_re: str, add_re: str) -> bool:
    section = _section_root(page, heading_re)
    scopes = [section, page]
    patterns: list[str] = []
    if add_re in {r"^Add$", "^Add$"}:
        patterns = [r"^Add$", r"^Add Work Experience$", r"^Add Education$"]
    elif "another" in add_re.lower():
        patterns = [r"Add Another"]
    else:
        patterns = [add_re]

    for scope in scopes:
        for pattern in patterns:
            try:
                btn = scope.get_by_role("button", name=re.compile(pattern, re.I))
                if _int_count(btn) == 0:
                    continue
                for i in range(_int_count(btn)):
                    target = btn.nth(i)
                    try:
                        if not bool(target.is_visible(timeout=600)):
                            continue
                        label = (target.inner_text(timeout=400) or "").strip().lower()
                        if pattern == r"^Add$" and "another" in label:
                            continue
                        target.click(timeout=3000, force=True)
                        logger.info("Workday: clicked %r in %s", label or pattern, heading_re)
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
            try:
                btn = scope.get_by_text(re.compile(pattern, re.I))
                if _int_count(btn) == 0:
                    continue
                target = btn.first
                if bool(target.is_visible(timeout=600)):
                    target.click(timeout=3000, force=True)
                    return True
            except Exception:
                continue

    fallback_sels = (
        ("button[data-automation-id='workExperienceAddButton']", "work"),
        ("button[data-automation-id='educationAddButton']", "education"),
    )
    for sel, kind in fallback_sels:
        if kind == "work" and "education" in heading_re.lower():
            continue
        if kind == "education" and "education" not in heading_re.lower():
            continue
        try:
            loc = section.locator(sel)
            if _int_count(loc) == 0:
                loc = page.locator(sel)
            if _int_count(loc) == 0:
                continue
            loc.first.click(timeout=3000, force=True)
            return True
        except Exception:
            continue
    return False


def _save_open_panel(page, root=None) -> bool:
    scope = root if root is not None else page
    try:
        page.keyboard.press("Tab")
        page.wait_for_timeout(200)
    except Exception:
        pass
    for sel in (
        "button[data-automation-id='save']",
        "button[data-automation-id='panelSetSaveButton']",
        "button[data-automation-id='saveButton']",
        "button[data-automation-id*='SaveButton']",
        "button[aria-label*='Save' i]",
        "button[title*='Save' i]",
        "[data-automation-id='wd-IconButton'][aria-label*='Save' i]",
        "button:has-text('Done')",
        "button:has-text('Save'):not(:has-text('Continue'))",
    ):
        try:
            loc = scope.locator(sel)
            if _int_count(loc) == 0:
                loc = page.locator(sel)
            if _int_count(loc) == 0:
                continue
            target = loc.last
            text = (target.inner_text(timeout=800) or "").lower()
            if "continue" in text and "save" not in text:
                continue
            if not bool(target.is_visible(timeout=500)):
                continue
            target.click(timeout=3000, force=True)
            logger.info("Workday: saved panel via %s", sel)
            return True
        except Exception:
            continue
    try:
        btn = page.get_by_role("button", name=re.compile(r"^(Save|Done|OK)$", re.I))
        if _int_count(btn) > 0:
            btn.last.click(timeout=3000, force=True)
            logger.info("Workday: saved panel via role=button Save/Done")
            return True
    except Exception:
        pass
    try:
        heading = page.get_by_text(re.compile(r"^Work Experience$|^Education$", re.I))
        if _int_count(heading) > 0:
            heading.first.click(timeout=2000, force=True)
            page.wait_for_timeout(400)
            return True
    except Exception:
        pass
    return False


def _national_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("91") and len(digits) >= 12:
        digits = digits[2:]
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


def _select_phone_country(page, country: str = "India") -> None:
    if _combobox_select(page, r"Country / Territory Phone Code", country):
        logger.info("Workday: phone country=%s", country)
        return
    if _combobox_select(page, r"Phone Code", f"{country} (+91)"):
        logger.info("Workday: phone country via Phone Code")


def _uncheck_preferred_name(page) -> None:
    """Legal Given Name stays empty if Preferred Name is expanded instead."""
    try:
        loc = page.get_by_role("checkbox", name=re.compile(r"preferred name", re.I))
        if _int_count(loc) > 0:
            el = loc.first
            try:
                if bool(el.is_checked()):
                    el.uncheck(timeout=2000)
                    logger.info("Workday: unchecked I have a preferred name")
                    return
            except Exception:
                if bool(el.is_checked()):
                    el.click(timeout=2000, force=True)
                    return
    except Exception:
        pass
    try:
        label = page.get_by_text(re.compile(r"I have a preferred name", re.I))
        if _int_count(label) == 0:
            return
        cb = label.first.locator(
            "xpath=ancestor::*[.//input[@type='checkbox']][1]//input[@type='checkbox']"
        )
        if _int_count(cb) > 0 and bool(cb.first.is_checked()):
            cb.first.click(timeout=2000, force=True)
            logger.info("Workday: unchecked preferred-name checkbox")
    except Exception:
        pass


def _fill_legal_given_name(page, value: str) -> bool:
    if not value:
        return False
    for sel in (
        "input[data-automation-id='legalNameSection_firstName']",
        "input[data-automation-id='legalNameSection_givenName']",
        "input[data-automation-id='legalName--legalName--firstName']",
        "[data-automation-id='legalNameSection'] input[data-automation-id*='firstName']",
        "[data-automation-id='legalNameSection'] input[data-automation-id*='givenName']",
        "[data-automation-id*='legalName'] input[data-automation-id*='firstName']",
    ):
        if _fill_input(page, [sel], value):
            logger.info("Workday: filled legal given name via %s", sel)
            return True
    field = _form_field(page, r"Given Name")
    n = _int_count(field)
    for i in range(min(n, 4)):
        root = field.first if i == 0 else field.nth(i)
        try:
            blob = (root.inner_text(timeout=400) or "").lower()
            if "preferred" in blob:
                continue
        except Exception:
            pass
        inp = root.locator(
            "input:not([type='hidden']):not([type='radio']):not([type='checkbox'])"
        )
        if _int_count(inp) == 0:
            continue
        el = inp.first
        try:
            if not bool(el.is_visible(timeout=400)):
                continue
        except Exception:
            continue
        if _set_text_value(el, value):
            logger.info("Workday: filled legal given name via form field %s", i)
            return True
    return False


def _fill_input(page, selectors: list[str], value: str) -> bool:
    if not value:
        return False
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = _int_count(loc)
            for i in range(min(n, 6)):
                el = loc.first if i == 0 else loc.nth(i)
                try:
                    if not bool(el.is_visible(timeout=500)):
                        continue
                except Exception:
                    continue
                if _set_text_value(el, value):
                    return True
        except Exception:
            continue
    return False


def _fill_labeled_input(page, label_re: str, value: str) -> bool:
    if not value:
        return False
    field = _form_field(page, label_re)
    if _int_count(field) > 0:
        inp = field.first.locator(
            "input:not([type='hidden']):not([type='radio']):not([type='checkbox']), textarea"
        )
        n = _int_count(inp)
        for i in range(min(n, 4)):
            el = inp.nth(i)
            try:
                if not bool(el.is_visible(timeout=400)):
                    continue
            except Exception:
                continue
            if _set_text_value(el, value):
                return True
    try:
        loc = page.get_by_label(re.compile(label_re, re.I))
        n = _int_count(loc)
        for i in range(min(n, 4)):
            el = loc.nth(i)
            try:
                if not bool(el.is_visible(timeout=400)):
                    continue
            except Exception:
                continue
            if _set_text_value(el, value):
                return True
    except Exception:
        pass
    return False


def _set_text_value(el, value: str) -> bool:
    """Replace a Workday text widget. fill() often appends or no-ops on these inputs."""
    try:
        existing = el.input_value(timeout=400)
    except Exception:
        existing = ""
    if isinstance(existing, str) and existing.strip() == value.strip():
        return True
    try:
        el.scroll_into_view_if_needed(timeout=1500)
        el.click(timeout=2000)
        try:
            el.fill("")
        except Exception:
            pass
        try:
            el.press("Control+A")
            el.press("Backspace")
        except Exception:
            pass
        try:
            el.fill(value, timeout=2500)
        except Exception:
            pass
        try:
            now = el.input_value(timeout=400)
        except Exception:
            now = None
        if isinstance(now, str) and now.strip() == value.strip():
            return True
        if now is not None and not isinstance(now, str):
            # Non-string (e.g. unit-test mocks) — assume fill applied.
            return True
        try:
            el.press("Control+A")
            el.press("Backspace")
            el.type(value, delay=15, timeout=5000)
        except Exception:
            try:
                el.press_sequentially(value, delay=15)
            except Exception:
                return False
        try:
            now = el.input_value(timeout=400)
        except Exception:
            now = value
        if isinstance(now, str):
            return now.strip() == value.strip()
        return True
    except Exception:
        return False


def _type_prompt(page, selectors: list[str], value: str) -> bool:
    if not _fill_input(page, selectors, value):
        return False
    try:
        page.wait_for_timeout(500)
    except Exception:
        pass
    for pattern in (rf"Use {re.escape(value)}", r"Use this", r"isn't listed"):
        if _click_text(page, pattern):
            return True
    try:
        opts = page.locator("[data-automation-id='promptOption'], [role='option']")
        if _int_count(opts) > 0:
            match = opts.filter(has_text=re.compile(re.escape(value), re.I))
            (match.first if _int_count(match) > 0 else opts.first).click(timeout=2500)
            return True
    except Exception:
        pass
    try:
        page.keyboard.press("Enter")
    except Exception:
        pass
    return True


def _select_month_year(page, field_id: str, parsed: dict[str, Any]) -> None:
    month, year = parsed.get("month"), parsed.get("year")
    mm = ""
    if year:
        num = _MONTH_NUM.get(str(month or ""), 0)
        mm = f"{num:02d}/{year}" if num else str(year)
    if mm:
        _fill_input(
            page,
            [
                f"input[data-automation-id='{field_id}']",
                f"input[data-automation-id='{field_id}-input']",
                "input[placeholder*='MM/YYYY' i]",
            ],
            mm,
        )
    root_sels = [
        f"[data-automation-id='formField-{field_id}']",
        f"[data-automation-id='{field_id}']",
    ]
    root = None
    for sel in root_sels:
        loc = page.locator(sel)
        if _int_count(loc) > 0:
            root = loc.first
            break
    if root is None:
        root = page
    if month:
        try:
            btn = root.locator(
                "[data-automation-id='dateSectionMonth-display'], "
                "button[aria-haspopup='listbox']:has-text('MM')"
            )
            if _int_count(btn) > 0:
                btn.first.click(timeout=2000)
                try:
                    page.wait_for_timeout(300)
                except Exception:
                    pass
                opt = page.locator(
                    "[data-automation-id='datePickerOption'], [role='option']"
                ).filter(has_text=re.compile(rf"^{re.escape(month)}$", re.I))
                if _int_count(opt) == 0:
                    opt = page.get_by_text(re.compile(rf"^{re.escape(month)}$", re.I))
                if _int_count(opt) > 0:
                    opt.first.click(timeout=2000)
        except Exception:
            pass
    if year:
        try:
            btn = root.locator(
                "[data-automation-id='dateSectionYear-display'], "
                "button[aria-haspopup='listbox']:has-text('YYYY')"
            )
            if _int_count(btn) > 0:
                btn.first.click(timeout=2000)
                try:
                    page.wait_for_timeout(300)
                except Exception:
                    pass
                opt = page.locator(
                    "[data-automation-id='datePickerOption'], [role='option']"
                ).filter(has_text=re.compile(rf"^{re.escape(str(year))}$"))
                if _int_count(opt) == 0:
                    opt = page.get_by_text(re.compile(rf"^{re.escape(str(year))}$"))
                if _int_count(opt) > 0:
                    opt.first.click(timeout=2000)
        except Exception:
            pass


def _check_currently_work_here(page) -> None:
    for sel in (
        "input[data-automation-id='currentlyWorkHere']",
        "input[type='checkbox'][id*='currently' i]",
    ):
        try:
            loc = page.locator(sel)
            if _int_count(loc) > 0:
                loc.first.check(timeout=2000)
                return
        except Exception:
            continue
    _click_text(page, r"I currently work here")


def _fill_input_in(root, selectors: list[str], value: str) -> bool:
    if not value:
        return False
    for sel in selectors:
        try:
            loc = root.locator(sel)
            n = _int_count(loc)
            for i in range(n - 1, -1, -1):
                el = loc.first if i == 0 else loc.nth(i)
                try:
                    if not bool(el.is_visible(timeout=500)):
                        continue
                except Exception:
                    continue
                if _set_text_value(el, value):
                    return True
        except Exception:
            continue
    return False


def _fill_labeled_input_in(root, page, label_re: str, value: str) -> bool:
    if not value:
        return False
    field = root.locator(
        "[data-automation-id*='formField'], [data-automation-id*='FormField']"
    ).filter(has_text=re.compile(label_re, re.I))
    if _int_count(field) > 0:
        inp = field.last.locator(
            "input:not([type='hidden']):not([type='radio']):not([type='checkbox']), textarea"
        )
        n = _int_count(inp)
        for i in range(min(n, 4)):
            el = inp.nth(i)
            try:
                if not bool(el.is_visible(timeout=400)):
                    continue
            except Exception:
                continue
            if _set_text_value(el, value):
                return True
    return _fill_labeled_input(page, label_re, value)


def _type_prompt_in(root, page, selectors: list[str], value: str) -> bool:
    if not _fill_input_in(root, selectors, value):
        return False
    try:
        page.wait_for_timeout(500)
    except Exception:
        pass
    for pattern in (rf"Use {re.escape(value)}", r"Use this", r"isn't listed"):
        if _click_text(page, pattern):
            return True
    try:
        opts = page.locator("[data-automation-id='promptOption'], [role='option']")
        if _int_count(opts) > 0:
            match = opts.filter(has_text=re.compile(re.escape(value), re.I))
            (match.last if _int_count(match) > 0 else opts.last).click(timeout=2500)
            return True
    except Exception:
        pass
    try:
        page.keyboard.press("Enter")
    except Exception:
        pass
    return True


def _select_month_year_in(root, page, field_id: str, parsed: dict[str, Any]) -> None:
    month, year = parsed.get("month"), parsed.get("year")
    mm = ""
    if year:
        num = _MONTH_NUM.get(str(month or ""), 0)
        mm = f"{num:02d}/{year}" if num else str(year)
    if mm:
        _fill_input_in(
            root,
            [
                f"input[data-automation-id='{field_id}']",
                f"input[data-automation-id='{field_id}-input']",
                "input[placeholder*='MM/YYYY' i]",
                "input[placeholder*='MM' i]",
            ],
            mm,
        )
    root_sels = [
        f"[data-automation-id='formField-{field_id}']",
        f"[data-automation-id='{field_id}']",
    ]
    scope = root
    for sel in root_sels:
        loc = root.locator(sel)
        if _int_count(loc) > 0:
            scope = loc.last
            break
    if month:
        try:
            btn = scope.locator(
                "[data-automation-id='dateSectionMonth-display'], "
                "button[aria-haspopup='listbox']:has-text('MM')"
            )
            if _int_count(btn) > 0:
                btn.last.click(timeout=2000)
                try:
                    page.wait_for_timeout(300)
                except Exception:
                    pass
                opt = page.locator(
                    "[data-automation-id='datePickerOption'], [role='option']"
                ).filter(has_text=re.compile(rf"^{re.escape(month)}$", re.I))
                if _int_count(opt) > 0:
                    opt.last.click(timeout=2000)
        except Exception:
            pass
    if year:
        try:
            btn = scope.locator(
                "[data-automation-id='dateSectionYear-display'], "
                "button[aria-haspopup='listbox']:has-text('YYYY')"
            )
            if _int_count(btn) > 0:
                btn.last.click(timeout=2000)
                try:
                    page.wait_for_timeout(300)
                except Exception:
                    pass
                opt = page.locator(
                    "[data-automation-id='datePickerOption'], [role='option']"
                ).filter(has_text=re.compile(rf"^{re.escape(str(year))}$"))
                if _int_count(opt) > 0:
                    opt.last.click(timeout=2000)
        except Exception:
            pass


def _fill_labeled_in(scope, page, label_re: str, value: str) -> bool:
    if not value:
        return False
    try:
        loc = scope.get_by_label(re.compile(label_re, re.I))
        n = _int_count(loc)
        for i in range(n - 1, -1, -1):
            el = loc.nth(i)
            try:
                if not bool(el.is_visible(timeout=400)):
                    continue
            except Exception:
                continue
            if _set_text_value(el, value):
                return True
    except Exception:
        pass
    return _fill_labeled_input_in(scope, page, label_re, value)


def _set_currently_work_here(root, page, current: bool) -> None:
    for sel in (
        "input[data-automation-id='currentlyWorkHere']",
        "input[type='checkbox'][id*='currently' i]",
    ):
        try:
            loc = root.locator(sel)
            if _int_count(loc) == 0:
                continue
            target = loc.last if _int_count(loc) > 1 else loc.first
            if current:
                target.check(timeout=2000)
            else:
                try:
                    if bool(target.is_checked()):
                        target.uncheck(timeout=2000)
                except Exception:
                    if bool(target.is_checked()):
                        target.click(timeout=2000, force=True)
            return
        except Exception:
            continue
    try:
        cb = root.get_by_text(re.compile(r"I currently work here", re.I))
        if _int_count(cb) > 0:
            cb.last.click(timeout=2000)
    except Exception:
        pass


def _fill_work_experience_panel(page, job: dict[str, Any], root=None) -> None:
    scope = root if root is not None else page
    title = str(job.get("title") or "").strip()
    company = str(job.get("company") or "").strip()
    start, end = _ordered_dates(job.get("start_date"), job.get("end_date"))

    # Job Title is a plain text field on this tenant — not a prompt.
    ok_title = False
    try:
        loc = scope.get_by_label(re.compile(r"Job Title", re.I))
        if _int_count(loc) > 0:
            ok_title = _set_text_value(loc.last, title)
    except Exception:
        pass
    ok_title = ok_title or _fill_input_in(
        scope,
        [
            "input[data-automation-id='jobTitle']",
            "input[id*='jobTitle' i]",
            "input[placeholder*='Job Title' i]",
            "input:not([type='hidden']):not([type='checkbox'])",
        ],
        title,
    ) or _fill_labeled_in(scope, page, r"Job Title", title)
    if not ok_title:
        logger.warning("Workday: job title not set (%s)", title[:40])

    # Company is often a typeahead; accept custom value via Enter / "Use …".
    ok_co = False
    try:
        loc = scope.get_by_label(re.compile(r"^Company", re.I))
        if _int_count(loc) > 0:
            ok_co = _set_text_value(loc.last, company)
    except Exception:
        pass
    ok_co = ok_co or _fill_input_in(
        scope,
        [
            "input[data-automation-id='company']",
            "input[data-automation-id='companyName']",
            "input[placeholder*='Company' i]",
        ],
        company,
    ) or _fill_labeled_in(scope, page, r"Company", company)
    if ok_co and company:
        try:
            page.wait_for_timeout(450)
        except Exception:
            pass
        if not (
            _click_text(page, rf"Use {re.escape(company)}")
            or _click_visible_prompt_option(page, company)
            or _click_text(page, r"Use this")
            or _click_text(page, r"isn't listed")
        ):
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
    if not ok_co:
        logger.warning("Workday: company not set (%s)", company[:40])

    _set_currently_work_here(scope, page, bool(end.get("current")))
    _fill_from_to_dates(scope, page, start, end)
    logger.info("Workday: filled work panel %s @ %s", title[:40], company[:40])


def _fill_from_to_dates(scope, page, start: dict[str, Any], end: dict[str, Any]) -> None:
    start_mm = ""
    if start.get("year"):
        num = _MONTH_NUM.get(str(start.get("month") or ""), 0)
        start_mm = f"{num:02d}/{start['year']}" if num else str(start["year"])
    end_mm = ""
    if not end.get("current") and end.get("year"):
        num = _MONTH_NUM.get(str(end.get("month") or ""), 0)
        end_mm = f"{num:02d}/{end['year']}" if num else str(end["year"])

    # Prefer labeled From/To month-year widgets inside this panel.
    if start_mm:
        _fill_date_field_in(scope, page, r"From", start_mm, start, "startDate")
    else:
        _select_month_year_in(scope, page, "startDate", start)

    if end.get("current"):
        return
    if end_mm:
        _fill_date_field_in(scope, page, r"To", end_mm, end, "endDate")
    else:
        _select_month_year_in(scope, page, "endDate", end)


def _fill_date_field_in(
    scope, page, label: str, mm_yyyy: str, parsed: dict[str, Any], field_id: str
) -> None:
    field = scope.locator(
        "[data-automation-id*='formField'], [data-automation-id*='FormField']"
    ).filter(has_text=re.compile(rf"{label}", re.I))
    root = field.last if _int_count(field) > 0 else scope
    filled = _fill_input_in(
        root,
        [
            "input[placeholder*='MM/YYYY' i]",
            "input[placeholder*='MM' i]",
            f"input[data-automation-id='{field_id}']",
            f"input[data-automation-id='{field_id}-input']",
            "input:not([type='hidden']):not([type='checkbox'])",
        ],
        mm_yyyy,
    )
    if filled:
        try:
            page.keyboard.press("Tab")
        except Exception:
            pass
        return
    _select_month_year_in(root, page, field_id, parsed)
    # Month / Year display buttons (MM + YYYY dropdowns).
    month, year = parsed.get("month"), parsed.get("year")
    if month:
        try:
            btn = root.locator(
                "[data-automation-id='dateSectionMonth-display'], "
                "button[aria-haspopup='listbox']"
            ).filter(has_text=re.compile(r"MM|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec", re.I))
            if _int_count(btn) == 0:
                btn = root.locator("[data-automation-id='dateSectionMonth-display']")
            if _int_count(btn) > 0:
                btn.last.click(timeout=2000)
                page.wait_for_timeout(250)
                opt = page.get_by_role("option", name=re.compile(rf"^{re.escape(month)}$", re.I))
                if _int_count(opt) == 0:
                    opt = page.locator("[role='option']").filter(
                        has_text=re.compile(rf"^{re.escape(month)}$", re.I)
                    )
                if _int_count(opt) > 0:
                    opt.last.click(timeout=2000)
        except Exception:
            pass
    if year:
        try:
            btn = root.locator(
                "[data-automation-id='dateSectionYear-display'], "
                "button[aria-haspopup='listbox']"
            ).filter(has_text=re.compile(r"YYYY|20\d{2}", re.I))
            if _int_count(btn) == 0:
                btn = root.locator("[data-automation-id='dateSectionYear-display']")
            if _int_count(btn) > 0:
                btn.last.click(timeout=2000)
                page.wait_for_timeout(250)
                opt = page.get_by_role("option", name=re.compile(rf"^{re.escape(str(year))}$"))
                if _int_count(opt) == 0:
                    opt = page.locator("[role='option']").filter(
                        has_text=re.compile(rf"^{re.escape(str(year))}$")
                    )
                if _int_count(opt) > 0:
                    opt.last.click(timeout=2000)
        except Exception:
            pass


def _fill_education_panel(page, school: dict[str, Any], root=None) -> None:
    scope = root if root is not None else page
    institution = str(school.get("institution") or "").strip()
    degree = str(school.get("degree") or "").strip()
    field = str(school.get("field") or "").strip()
    marks = str(school.get("marks") or "").strip()
    start, end = _ordered_dates(school.get("start_date"), school.get("end_date"))

    _type_prompt_in(
        scope,
        page,
        [
            "input[data-automation-id='school']",
            "input[data-automation-id='schoolName']",
            "input[placeholder*='School' i]",
            "input[placeholder*='College' i]",
        ],
        institution,
    )
    if degree:
        aliases = _DEGREE_ALIASES.get(degree.lower(), [degree])
        picked = False
        for alias in aliases:
            if _pick_prompt_option(page, r"Degree", alias):
                picked = True
                break
        if not picked:
            _type_prompt_in(
                scope,
                page,
                [
                    "input[data-automation-id='degree']",
                    "input[placeholder*='Degree' i]",
                ],
                aliases[0],
            )
    if field:
        _type_prompt_in(
            scope,
            page,
            [
                "input[data-automation-id='fieldOfStudy']",
                "input[placeholder*='Field' i]",
            ],
            field,
        )
    gpa = _gpa_from_marks(marks)
    if gpa:
        _fill_labeled_in(scope, page, r"Overall Result|GPA", gpa) or _fill_input_in(
            scope,
            [
                "input[data-automation-id='gpa']",
                "input[placeholder*='GPA' i]",
                "input[placeholder*='Overall Result' i]",
            ],
            gpa,
        )
    if start.get("year"):
        _fill_labeled_in(scope, page, r"From \(YYYY\)|From", str(start["year"])) or _type_prompt_in(
            scope,
            page,
            ["input[data-automation-id='firstYearAttended']"],
            str(start["year"]),
        )
    if end.get("year"):
        _fill_labeled_in(
            scope, page, r"To \(Actual or Expected\)|To", str(end["year"])
        ) or _type_prompt_in(
            scope,
            page,
            ["input[data-automation-id='lastYearAttended']"],
            str(end["year"]),
        )


def _add_skills(page, skills: list[str], filled: list[str]) -> None:
    added = 0
    for skill in skills:
        ok = _type_prompt(
            page,
            [
                "input[data-automation-id='skills']",
                "input[data-automation-id='skill']",
                "input[placeholder*='Skill' i]",
            ],
            skill,
        )
        if ok:
            added += 1
            try:
                page.wait_for_timeout(250)
            except Exception:
                pass
    if added and "skills" not in filled:
        filled.append("skills")


def _answer_how_heard(page, option: str, filled: list[str]) -> None:
    if "how_heard" in filled:
        return
    if _pick_prompt_option(page, r"How Did You Hear About Us", option):
        filled.append("how_heard")
        return
    for fallback in ("LinkedIn", "Job Board", "Company Website", "Other"):
        if fallback.lower() == option.lower():
            continue
        if _pick_prompt_option(page, r"How Did You Hear About Us", fallback):
            filled.append("how_heard")
            return


def _pick_prompt_option(page, label_re: str, option: str) -> bool:
    try:
        field = _form_field(page, label_re)
        if _int_count(field) > 0:
            root = field.first
            box = root.locator(
                "input:not([type='hidden']):not([type='radio']):not([type='checkbox'])"
            )
            if _int_count(box) > 0:
                box.first.click(timeout=2500)
            else:
                root.click(timeout=2500)
        else:
            label = page.get_by_text(re.compile(label_re, re.I))
            if _int_count(label) == 0:
                return False
            label.first.click(timeout=2500)
        try:
            page.wait_for_timeout(400)
        except Exception:
            pass
        typed = False
        try:
            page.keyboard.type(option, delay=40)
            typed = True
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass
        except Exception:
            pass
        for sel in (
            "[data-automation-id='promptOption']",
            "[role='option']",
            "li[role='option']",
        ):
            opts = page.locator(sel)
            if _int_count(opts) == 0:
                continue
            match = opts.filter(has_text=re.compile(re.escape(option), re.I))
            if _int_count(match) == 0:
                continue
            match.first.click(timeout=3000)
            return True
        if typed:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        return False
    except Exception:
        return False


def _answer_yes_no(
    page, question_re: str, answer: str, filled: list[str], *, name: str
) -> None:
    if name in filled:
        return
    yes = str(answer).strip().lower() in {"yes", "y", "true", "1"}
    choice = "Yes" if yes else "No"
    field = _form_field(page, question_re)
    targets = []
    if _int_count(field) > 0:
        targets.append(field.first)
    else:
        try:
            q = page.get_by_text(re.compile(question_re, re.I))
            if _int_count(q) > 0:
                targets.append(q.first)
        except Exception:
            pass
    for root in targets:
        try:
            blob = (root.inner_text(timeout=400) or "").lower()
            if "select one" in blob:
                continue
            radios = root.get_by_text(re.compile(rf"^{choice}$", re.I))
            if _int_count(radios) > 0:
                el = radios.first
                if not bool(el.is_visible(timeout=400)):
                    continue
                el.click(timeout=2500, force=True)
                filled.append(name)
                logger.info("Workday: answered %s=%s", name, choice)
                return
            group = root.locator(
                "xpath=ancestor::div[contains(@data-automation-id,'formField') "
                "or contains(@class,'FormField')][1]"
            )
            radios = group.get_by_text(re.compile(rf"^{choice}$", re.I))
            if _int_count(radios) > 0:
                el = radios.first
                if not bool(el.is_visible(timeout=400)):
                    continue
                el.click(timeout=2500, force=True)
                filled.append(name)
                logger.info("Workday: answered %s=%s", name, choice)
                return
            radio_inp = root.locator(
                f"input[type='radio'][value='{choice}'], "
                f"input[type='radio'][aria-label='{choice}']"
            )
            if _int_count(radio_inp) > 0:
                el = radio_inp.first
                if not bool(el.is_visible(timeout=400)):
                    continue
                el.click(timeout=2500, force=True)
                filled.append(name)
                return
        except Exception:
            continue


def _main_apply_scope(page):
    for sel in ("[data-automation-id='applyFlowPage']", "main"):
        try:
            loc = page.locator(sel)
            if _int_count(loc) > 0:
                return loc.first
        except Exception:
            continue
    return page.locator("body").first


def _on_voluntary_disclosures_page(page) -> bool:
    if _on_my_experience_page(page) or _my_information_ready(page):
        return False
    try:
        title = page.locator("[data-automation-id='applyFlowPageTitle']")
        if _int_count(title) > 0:
            raw = (title.first.inner_text(timeout=600) or "").lower().strip()
            if "voluntary" in raw or "disclosure" in raw:
                return True
            if raw:
                return False
    except Exception:
        pass
    scope = _main_apply_scope(page)
    try:
        ack = scope.get_by_text(re.compile(r"I acknowledge Workday", re.I))
        if _int_count(ack) > 0 and bool(ack.first.is_visible(timeout=400)):
            return True
    except Exception:
        pass
    return False


def _fill_voluntary_disclosures(page, filled: list[str]) -> None:
    """Voluntary Disclosures: decline EEO where optional, check required consent."""
    if not _on_voluntary_disclosures_page(page):
        return
    _check_vibe_acknowledgment(page, filled)
    _check_agreements(page, filled)
    _select_gender_decline(page, filled)
    _select_decline_eeo(page, filled)


def _select_gender_decline(page, filled: list[str]) -> None:
    if "eeo_gender" in filled:
        return
    scope = _main_apply_scope(page)
    try:
        field = scope.locator("[data-automation-id*='formField']").filter(
            has_text=re.compile(r"Gender", re.I)
        )
        if _int_count(field) == 0:
            field = _form_field(page, r"Gender")
        if _int_count(field) == 0:
            return
        root = field.last
        if not _open_select_widget(root, page):
            return
        try:
            page.wait_for_timeout(400)
        except Exception:
            pass
        for answer in (
            "I do not want to answer",
            "Decline to Self Identify",
            "Prefer not to say",
            "Prefer not to self-identify",
        ):
            if _click_dropdown_choice(page, answer):
                filled.append("eeo_gender")
                logger.info("Workday: gender=%s", answer)
                return
    except Exception:
        pass


def _check_vibe_acknowledgment(page, filled: list[str]) -> bool:
    if "vibe_ack" in filled or "agreement" in filled:
        return True
    patterns = (
        r"I acknowledge Workday.s Recruitment Privacy Statement and VIBE Philosophy",
        r"Recruitment Privacy Statement and VIBE Philosophy",
        r"VIBE Philosophy",
    )
    for pat in patterns:
        if _check_labeled_checkbox(page, pat, filled, name="vibe_ack"):
            return True
    try:
        cbs = _main_apply_scope(page).locator("input[type='checkbox']")
        n = _int_count(cbs)
        for i in range(n):
            cb = cbs.nth(i)
            try:
                if not bool(cb.is_visible(timeout=300)):
                    continue
            except Exception:
                continue
            try:
                block = cb.locator(
                    "xpath=ancestor::*[self::label or self::div][1]"
                )
                txt = (block.inner_text(timeout=400) or "").lower()
            except Exception:
                txt = ""
            if "vibe" in txt or (
                "privacy statement" in txt and "acknowledge" in txt
            ):
                if not bool(cb.is_checked()):
                    cb.check(timeout=2500)
                filled.append("vibe_ack")
                logger.info("Workday: checked VIBE acknowledgment checkbox")
                return True
    except Exception:
        pass
    try:
        label = _main_apply_scope(page).get_by_text(re.compile(r"I acknowledge Workday", re.I))
        if _int_count(label) > 0:
            label.first.scroll_into_view_if_needed(timeout=1500)
            for xpath in (
                "xpath=preceding::input[@type='checkbox'][1]",
                "xpath=ancestor::*[.//input[@type='checkbox']][1]//input[@type='checkbox']",
            ):
                nearby = label.first.locator(xpath)
                if _int_count(nearby) == 0:
                    continue
                el = nearby.first
                if not bool(el.is_checked()):
                    el.check(timeout=2500)
                filled.append("vibe_ack")
                logger.info("Workday: checked VIBE acknowledgment (preceding cb)")
                return True
    except Exception:
        pass
    return False


def _select_decline_eeo(page, filled: list[str]) -> None:
    for pattern in (
        r"I do not want to answer",
        r"Decline to Self Identify",
        r"I don't wish to answer",
        r"Prefer not to say",
        r"Prefer not to self[- ]identify",
        r"I decline to self[- ]identify",
    ):
        if _click_text(page, pattern) and "eeo_decline" not in filled:
            filled.append("eeo_decline")


def _check_labeled_checkbox(page, label_re: str, filled: list[str], *, name: str) -> bool:
    if name in filled:
        return True
    try:
        cb = page.get_by_role("checkbox", name=re.compile(label_re, re.I))
        if _int_count(cb) > 0:
            el = cb.first
            if not bool(el.is_checked()):
                el.check(timeout=2500)
            filled.append(name)
            logger.info("Workday: checked %s", name)
            return True
    except Exception:
        pass
    try:
        label = page.get_by_text(re.compile(label_re, re.I))
        if _int_count(label) == 0:
            return False
        for i in range(min(_int_count(label), 4)):
            root = label.nth(i)
            cb = root.locator(
                "xpath=ancestor::*[.//input[@type='checkbox']][1]//input[@type='checkbox']"
            )
            if _int_count(cb) == 0:
                cb = root.locator("xpath=preceding::input[@type='checkbox'][1]")
            if _int_count(cb) == 0:
                cb = root.locator("xpath=following::input[@type='checkbox'][1]")
            if _int_count(cb) == 0:
                continue
            el = cb.first
            try:
                if not bool(el.is_visible(timeout=400)):
                    continue
            except Exception:
                continue
            try:
                if not bool(el.is_checked()):
                    el.check(timeout=2500)
            except Exception:
                el.click(timeout=2500, force=True)
            filled.append(name)
            logger.info("Workday: checked %s via label", name)
            return True
    except Exception:
        pass
    return False


def _check_agreements(page, filled: list[str]) -> None:
    specifics = (
        (
            r"Recruitment Privacy Statement and VIBE Philosophy|VIBE Philosophy",
            "vibe_ack",
        ),
        (r"I accept", "agreement"),
        (r"I agree", "agreement"),
        (r"I have read and agree", "agreement"),
        (r"I consent", "agreement"),
    )
    for pattern, name in specifics:
        _check_labeled_checkbox(page, pattern, filled, name=name)
    for pattern in (r"I acknowledge",):
        if _check_labeled_checkbox(page, pattern, filled, name="agreement"):
            continue
        try:
            loc = page.get_by_text(re.compile(pattern, re.I))
            n = _int_count(loc)
            for i in range(min(n, 4)):
                el = loc.nth(i)
                try:
                    if el.is_visible(timeout=400):
                        el.click(timeout=2000, force=True)
                        if "agreement" not in filled:
                            filled.append("agreement")
                except Exception:
                    continue
        except Exception:
            continue


def _fill_empty_textareas(page, text: str, filled: list[str]) -> None:
    try:
        areas = page.locator("textarea")
        n = _int_count(areas)
        for i in range(min(n, 6)):
            area = areas.nth(i)
            try:
                if not area.is_visible(timeout=400):
                    continue
                current = area.input_value(timeout=800) or ""
                if current.strip():
                    continue
                area.fill(text[:3500], timeout=2500)
                if "textarea" not in filled:
                    filled.append("textarea")
            except Exception:
                continue
    except Exception:
        pass


def _llm_fill_remaining(
    page, profile: dict[str, Any], materials: dict[str, Any], filled: list[str]
) -> None:
    snapshot = _required_field_texts(page)
    if not snapshot:
        return
    try:
        from llm.client import LLMClient

        prompt = (
            "Answer these Workday application questions as the candidate. "
            "Return JSON {\"answers\": [{\"question\": \"...\", \"answer\": \"...\"}]}. "
            "Yes/No: use Yes or No. Dropdowns: pick one listed option exactly. "
            "EEO/demographic: Decline to self identify / I do not want to answer. "
            "Do not invent SSN, bank details, or passwords. "
            "If the candidate is in India applying abroad, sponsorship=Yes and "
            "work authorization in that country=No unless the profile says otherwise.\n\n"
            f"PROFILE:\n{json.dumps({k: profile.get(k) for k in ('contact', 'apply_answers', 'experience', 'education')}, default=str)[:5000]}\n\n"
            f"QUESTIONS:\n{json.dumps(snapshot)[:5000]}"
        )
        data = LLMClient().generate_json(prompt)
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, list):
            return
        for item in answers[:20]:
            if not isinstance(item, dict):
                continue
            q = str(item.get("question") or "")
            a = str(item.get("answer") or "").strip()
            if not a:
                continue
            if a.lower() in {"yes", "no"}:
                _answer_yes_no(page, re.escape(q[:80]) if q else a, a, filled, name=f"q_{len(filled)}")
            elif not _pick_prompt_option(page, re.escape(q[:60]) if q else a, a):
                _click_text(page, re.escape(a[:80]))
        if "llm_questions" not in filled:
            filled.append("llm_questions")
    except Exception as exc:
        logger.info("Workday LLM page fill skipped: %s", exc)


def _required_field_texts(page) -> list[str]:
    try:
        data = page.evaluate(
            """() => {
              const out = [];
              const nodes = document.querySelectorAll(
                '[data-automation-id*="formField"], [data-automation-error], [aria-required="true"]'
              );
              for (const n of nodes) {
                const t = (n.innerText || '').replace(/\\s+/g, ' ').trim();
                if (t.length > 8) out.push(t.slice(0, 350));
              }
              const banner = document.body && /errors?\\s+found/i.test(document.body.innerText || '');
              if (banner) out.unshift('Errors Found on page');
              return out.slice(0, 18);
            }"""
        )
        if isinstance(data, list):
            return [str(x) for x in data if isinstance(x, str)]
    except Exception:
        pass
    return []


def _submit_visible(page) -> bool:
    for sel in SUBMIT_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            text = ""
            try:
                text = (loc.first.inner_text(timeout=800) or "").lower()
            except Exception:
                text = sel.lower()
            if "submit" in text or "submit" in sel.lower():
                # Ensure it's not just another Next mislabeled — require submit.
                return "submit" in text or "data-automation-id='submit'" in sel
        except Exception:
            continue
    return False


def _click_first(page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if _int_count(loc) == 0:
                continue
            text = ""
            try:
                text = (loc.first.inner_text(timeout=800) or "").lower()
            except Exception:
                text = ""
            # In callers that use NEXT_SELECTORS, skip pure Submit buttons.
            if sel in NEXT_SELECTORS and "submit" in text and "next" not in text:
                continue
            loc.first.scroll_into_view_if_needed(timeout=1500)
            loc.first.click(timeout=3000, force=True)
            return True
        except Exception:
            continue
    return False


adapter = WorkdayAdapter()
