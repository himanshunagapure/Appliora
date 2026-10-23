"""
Greenhouse application adapter.

Handles both boards.greenhouse.io and company career pages that embed
Greenhouse via ?gh_jid=… / job-boards.greenhouse.io iframes (e.g. Precisely).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from automation.cookies import dismiss_cookie_banners
from automation.form_detector import profile_value_map
from automation.types import FillResult, FormPlan
from utils.logger import get_logger

logger = get_logger("jobapplypro.adapter.greenhouse")

APPLY_SELECTORS = [
    "a#apply_button",
    "button#apply_button",
    "a[href*='#app']",
    "a:has-text('Apply for this job')",
    "button:has-text('Apply for this job')",
    "[data-qa='btn-apply']",
    # Broader Apply last — filtered to skip hidden filter buttons.
    "a:has-text('Apply')",
    "button:has-text('Apply')",
]


class GreenhouseAdapter:
    name = "greenhouse"

    def matches(self, url: str, html: str = "") -> bool:
        host = (urlparse(url).hostname or "").lower()
        query = parse_qs(urlparse(url).query)
        if "gh_jid" in query or "gh_jid=" in (url or "").lower():
            return True
        if "greenhouse.io" in host or "job-boards.greenhouse.io" in host:
            return True
        lower = (html or "").lower()
        return "greenhouse" in lower and (
            "application_form" in lower
            or "gh_jid" in lower
            or "grnhse" in lower
            or "job-boards.greenhouse" in lower
        )

    def fill(
        self,
        page,
        plan: FormPlan,
        profile: dict[str, Any],
        materials: dict[str, Any],
        *,
        dry_run: bool = True,
    ) -> FillResult:
        dismiss_cookie_banners(page)

        # Job description pages need an Apply click to reach the form.
        # Embedded job-boards iframes already show the form — skip Apply then.
        if not _has_greenhouse_form(page):
            _click_job_apply(page)
            try:
                page.wait_for_timeout(1200)
            except Exception:
                pass
            dismiss_cookie_banners(page)

        values = profile_value_map(profile)
        filled: list[str] = []
        skipped: list[str] = []

        target = _wait_for_greenhouse_form(page)
        if target is page and not _frame_has_fields(page):
            # Still wait a bit — embed can load after cookie dismiss.
            try:
                page.wait_for_timeout(2000)
            except Exception:
                pass
            target = _wait_for_greenhouse_form(page)

        field_tries = [
            ("first_name", "input#first_name"),
            ("first_name", "input[name='job_application[first_name]']"),
            ("last_name", "input#last_name"),
            ("last_name", "input[name='job_application[last_name]']"),
            ("email", "input#email"),
            ("email", "input[name='job_application[email]']"),
            ("phone", "input#phone"),
            ("phone", "input[name='job_application[phone]']"),
            ("location", "input#candidate-location"),
            ("location", "input#country"),
            ("linkedin_url", "input[autocomplete='custom-question-linkedin-profile']"),
            ("cover_letter", "textarea#cover_letter"),
            ("cover_letter", "textarea[name='job_application[cover_letter_text]']"),
        ]
        for logical, sel in field_tries:
            if logical in filled:
                continue
            val = values.get(logical)
            if logical == "cover_letter":
                val = materials.get("cover_letter_text") or val
            if not val:
                continue
            try:
                target.fill(sel, str(val), timeout=2500)
                filled.append(logical)
            except Exception:
                skipped.append(logical)

        resume_path = materials.get("resume_path")
        if resume_path and Path(resume_path).exists():
            try:
                from automation.resume_upload import ensure_uploadable_resume

                resume_path = str(ensure_uploadable_resume(resume_path))
            except Exception as exc:
                logger.warning("Greenhouse resume convert skipped: %s", exc)
            for sel in (
                "input#resume",
                "input[type='file']#resume",
                "input[name='job_application[resume]']",
                "input[type='file']",
            ):
                try:
                    target.set_input_files(sel, str(resume_path))
                    filled.append("resume")
                    break
                except Exception:
                    continue

        # New Greenhouse boards often use a file input for cover letter.
        cl_path = materials.get("cover_letter_path")
        if cl_path and Path(cl_path).exists():
            for sel in (
                "input#cover_letter",
                "input[type='file']#cover_letter",
                "input[name='job_application[cover_letter]']",
            ):
                try:
                    target.set_input_files(sel, str(cl_path))
                    filled.append("cover_letter")
                    break
                except Exception:
                    continue

        if not dry_run:
            # HTML scans miss React submit buttons. Try the detected selector,
            # then the live button text Greenhouse actually renders.
            selectors: list[str] = []
            if plan.submit_button:
                selectors.append(plan.submit_button)
            for extra in (
                "button:has-text('Submit application')",
                "button:has-text('Submit Application')",
                "button:has-text('Submit')",
                "input[type='submit']",
            ):
                if extra not in selectors:
                    selectors.append(extra)
            last_exc: Exception | None = None
            for sel in selectors:
                try:
                    target.click(sel, timeout=8000)
                    return FillResult(
                        status="submitted",
                        fields_filled=filled,
                        fields_skipped=skipped,
                        adapter=self.name,
                    )
                except Exception as exc:
                    last_exc = exc
                    continue
            if last_exc is not None and (filled or plan.submit_button):
                return FillResult(
                    status="failed",
                    error=f"Greenhouse submit failed: {last_exc}",
                    fields_filled=filled,
                    fields_skipped=skipped,
                    adapter=self.name,
                )

        if not filled:
            return FillResult(
                status="manual_review",
                error="greenhouse: no fillable fields found after cookie dismiss / apply",
                fields_filled=filled,
                fields_skipped=skipped,
                adapter=self.name,
            )

        return FillResult(
            status="filled",
            fields_filled=filled,
            fields_skipped=skipped,
            adapter=self.name,
        )


def _click_job_apply(page) -> bool:
    for sel in APPLY_SELECTORS:
        try:
            loc = page.locator(sel)
            count = loc.count()
            if count == 0:
                continue
            for i in range(min(count, 6)):
                candidate = loc.nth(i)
                try:
                    if not candidate.is_visible(timeout=800):
                        continue
                    text = (candidate.inner_text(timeout=500) or "").lower()
                    el_id = (candidate.get_attribute("id") or "").lower()
                    if "filter" in el_id or "filter" in text:
                        continue
                    candidate.click(timeout=4000, force=True)
                    logger.info("Clicked Greenhouse/job Apply via %s", sel)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _greenhouse_frames(page):
    try:
        for frame in page.frames:
            url = (frame.url or "").lower()
            if "greenhouse" in url or "grnhse" in url:
                yield frame
    except Exception:
        return


def _frame_has_fields(root) -> bool:
    for sel in ("input#first_name", "input#email", "input[name='job_application[first_name]']"):
        try:
            if root.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def _has_greenhouse_form(page) -> bool:
    if _frame_has_fields(page):
        return True
    for frame in _greenhouse_frames(page):
        if _frame_has_fields(frame):
            return True
    return False


def _wait_for_greenhouse_form(page, *, attempts: int = 12, delay_ms: int = 500):
    """Return Frame or Page that hosts first_name/email fields."""
    for i in range(attempts):
        for frame in _greenhouse_frames(page):
            if _frame_has_fields(frame):
                logger.info("Using Greenhouse iframe: %s", frame.url)
                return frame
        if _frame_has_fields(page):
            return page
        if i + 1 < attempts:
            try:
                page.wait_for_timeout(delay_ms)
            except Exception:
                break
    # Fall back to any greenhouse frame even without detected fields.
    for frame in _greenhouse_frames(page):
        logger.info("Using Greenhouse iframe (no fields yet): %s", frame.url)
        return frame
    return page


adapter = GreenhouseAdapter()
