"""
Generic HTML application form adapter (fallback — covers the most ground).

Fills mapped fields from FormPlan, uploads resume/cover letter files when
selectors exist, advances Next/Continue on multi-step forms, and NEVER clicks
the final Submit button when dry_run=True.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from automation.cookies import dismiss_cookie_banners
from automation.form_detector import profile_value_map
from automation.types import FillResult, FormPlan
from utils.logger import get_logger

logger = get_logger("jobapplypro.adapter.generic")

NEXT_SELECTORS = [
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "button:has-text('Save and Continue')",
    "button:has-text('Review')",
]

FINAL_SUBMIT_TEXTS = (
    "submit application",
    "submit your application",
    "send application",
)

FINAL_SUBMIT_EXACT = {
    "submit",
    "submit application",
    "submit your application",
    "send application",
}


class GenericAdapter:
    name = "generic"

    def matches(self, url: str, html: str = "") -> bool:
        return True  # always available as fallback

    def fill(
        self,
        page,
        plan: FormPlan,
        profile: dict[str, Any],
        materials: dict[str, Any],
        *,
        dry_run: bool = True,
    ) -> FillResult:
        values = profile_value_map(profile)
        cover_text = materials.get("cover_letter_text") or ""
        resume_path = materials.get("resume_path")
        cover_path = materials.get("cover_letter_path")

        filled: list[str] = []
        skipped: list[str] = []

        dismiss_cookie_banners(page)

        if plan.apply_button:
            try:
                loc = page.locator(plan.apply_button).first
                if loc.count() > 0 and loc.is_visible(timeout=1500):
                    # Avoid hidden filter "Apply" buttons (e.g. id=filter-apply-handler).
                    el_id = (loc.get_attribute("id") or "").lower()
                    if "filter" not in el_id:
                        loc.click(timeout=5000, force=True)
                else:
                    logger.info("Apply button not visible — skipping (%s)", plan.apply_button)
            except Exception as exc:
                logger.info("Apply button click skipped: %s", exc)

        for field in plan.fields:
            try:
                # Skip honeypot / clearly unsafe field names.
                blob = f"{field.name} {field.selector}".lower()
                if any(x in blob for x in ("ssn", "social_security", "credit_card", "cvv", "password")):
                    skipped.append(field.name)
                    logger.warning("Refusing to fill sensitive field: %s", field.selector)
                    continue

                if field.field_type == "file":
                    path = None
                    if field.name == "resume" and resume_path:
                        path = Path(resume_path)
                        try:
                            from automation.resume_upload import ensure_uploadable_resume

                            path = ensure_uploadable_resume(path)
                        except Exception as exc:
                            logger.warning("Generic resume convert skipped: %s", exc)
                    elif field.name == "cover_letter" and cover_path:
                        path = Path(cover_path)
                    if path and path.exists():
                        page.set_input_files(field.selector, str(path))
                        filled.append(field.name)
                    else:
                        skipped.append(field.name)
                    continue

                if field.name == "cover_letter" and cover_text:
                    _safe_fill(page, field.selector, cover_text)
                    filled.append(field.name)
                    continue

                value = values.get(field.name)
                if not value:
                    skipped.append(field.name)
                    continue

                if field.field_type == "select":
                    try:
                        page.select_option(field.selector, label=value)
                    except Exception:
                        page.select_option(field.selector, value=value)
                elif field.field_type in {"checkbox", "radio"}:
                    if value.lower() in {"yes", "true", "1", "y"}:
                        page.check(field.selector)
                    filled.append(field.name)
                    continue
                else:
                    _safe_fill(page, field.selector, value)
                filled.append(field.name)
            except Exception as exc:
                logger.warning(
                    "Could not fill %s (%s): %s", field.name, field.selector, exc
                )
                skipped.append(field.name)

        # Always attempt safe page advance when multi_page OR when Next exists;
        # never click final Submit in dry-run.
        if plan.multi_page:
            _advance_pages(page, dry_run=dry_run)
        elif dry_run and _final_submit_visible(page):
            logger.info("Dry-run: Submit visible on single-page form — not clicking")

        if not dry_run and plan.submit_button:
            try:
                _safe_click(page, plan.submit_button)
                return FillResult(
                    status="submitted",
                    fields_filled=filled,
                    fields_skipped=skipped,
                    adapter=self.name,
                )
            except Exception as exc:
                return FillResult(
                    status="failed",
                    error=f"Submit click failed: {exc}",
                    fields_filled=filled,
                    fields_skipped=skipped,
                    adapter=self.name,
                )

        if not filled and not plan.fields:
            return FillResult(
                status="manual_review",
                error="unrecognized_form: no fillable fields mapped",
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


def _safe_fill(page, selector: str, value: str) -> None:
    page.fill(selector, value)


def _safe_click(page, selector: str) -> None:
    page.click(selector)


def _is_final_submit_label(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if t in FINAL_SUBMIT_EXACT:
        return True
    if any(s in t for s in FINAL_SUBMIT_TEXTS):
        return True
    if "submit" in t and "next" not in t and "continue" not in t and "review" not in t:
        return True
    return False


def _final_submit_visible(page) -> bool:
    for sel in (
        "button:has-text('Submit application')",
        "button:has-text('Submit Application')",
        "button:has-text('Submit')",
        "input[type='submit']",
    ):
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            try:
                label = (loc.first.inner_text(timeout=500) or "").strip()
            except Exception:
                label = "submit"
            if _is_final_submit_label(label) or sel.startswith("input[type='submit']"):
                return True
        except Exception:
            continue
    return False


def _advance_pages(page, *, dry_run: bool, max_steps: int = 6) -> None:
    for _ in range(max_steps):
        if dry_run and _final_submit_visible(page):
            logger.info("Dry-run: final Submit visible — stopping page advance")
            return

        clicked = False
        for sel in NEXT_SELECTORS:
            try:
                loc = page.locator(sel)
                if loc.count() == 0:
                    continue
                label = ""
                try:
                    label = loc.first.inner_text(timeout=1000)
                except Exception:
                    label = sel
                if dry_run and _is_final_submit_label(label):
                    logger.info("Dry-run: refusing to click final submit (%s)", label)
                    return
                loc.first.click(timeout=3000)
                clicked = True
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                break
            except Exception:
                continue
        if not clicked:
            return


adapter = GenericAdapter()
