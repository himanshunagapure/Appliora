"""
Dismiss cookie / consent banners that block apply forms.

Many career sites (Precisely, Workday hosts, etc.) show OneTrust / Cookiebot /
generic "Accept all" modals before the application UI is usable.
"""

from __future__ import annotations

from utils.logger import get_logger

logger = get_logger("jobapplypro.cookies")

# Prefer "accept all" style buttons so the form becomes usable.
COOKIE_ACCEPT_SELECTORS = [
    # OneTrust (Precisely and many career sites)
    "#onetrust-accept-btn-handler",
    "button#onetrust-accept-btn-handler",
    "#accept-recommended-btn-handler",
    "button:has-text('Accept All Cookies')",
    "button:has-text('Accept all cookies')",
    # Cookiebot
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#CybotCookiebotDialogBodyButtonAccept",
    # Common generic
    "button:has-text('Accept All')",
    "button:has-text('Accept all')",
    "button:has-text('Allow all')",
    "button:has-text('Allow All')",
    "button:has-text('I Agree')",
    "button:has-text('I agree')",
    "button:has-text('Agree')",
    "button:has-text('Accept Cookies')",
    "button:has-text('Accept cookies')",
    "button:has-text('Accept')",
    "a:has-text('Accept All')",
    "a:has-text('Accept all')",
    "[aria-label='Accept cookies']",
    "[aria-label='Accept all']",
    "[data-testid='cookie-accept']",
    "button[mode='primary']:has-text('Accept')",
]

# Closing the banner via Reject is better than leaving it blocking the form.
COOKIE_REJECT_SELECTORS = [
    "#onetrust-reject-all-handler",
    "button#onetrust-reject-all-handler",
    "button:has-text('Reject All Cookies')",
    "button:has-text('Reject all cookies')",
    "button:has-text('Reject All')",
    "button:has-text('Reject all')",
    "button:has-text('Decline')",
    "button:has-text('Deny')",
    "button.onetrust-close-btn-handler",
]


def dismiss_cookie_banners(page, *, timeout_ms: int = 2500) -> bool:
    """
    Try to dismiss a cookie/consent banner. Returns True if something was clicked.
    Safe to call even when no banner is present.
    """
    # Banner scripts often inject a beat after DOMContentLoaded.
    for _attempt in range(3):
        clicked = _try_click_selectors(page, COOKIE_ACCEPT_SELECTORS, timeout_ms)
        if not clicked:
            clicked = _try_click_selectors(page, COOKIE_REJECT_SELECTORS, timeout_ms)
        if clicked:
            return True
        try:
            page.wait_for_timeout(700)
        except Exception:
            break

    # Some sites use a shadow-DOM / iframe OneTrust host.
    try:
        frame = page.frame_locator("#onetrust-banner-sdk, #onetrust-consent-sdk")
        btn = frame.locator(
            "#onetrust-accept-btn-handler, #onetrust-reject-all-handler, "
            "button:has-text('Accept All Cookies'), button:has-text('Accept')"
        )
        if btn.count() > 0:
            btn.first.click(timeout=timeout_ms, force=True)
            logger.info("Dismissed cookie banner inside OneTrust frame")
            try:
                page.wait_for_timeout(800)
            except Exception:
                pass
            return True
    except Exception:
        pass

    return False


def _try_click_selectors(page, selectors: list[str], timeout_ms: int) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if not loc.is_visible(timeout=timeout_ms):
                continue
            loc.click(timeout=timeout_ms, force=True)
            logger.info("Dismissed cookie/consent banner via %s", sel)
            try:
                page.wait_for_timeout(800)
            except Exception:
                pass
            return True
        except Exception:
            continue
    return False
