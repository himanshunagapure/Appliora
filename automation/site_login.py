"""
Password login for job boards when the persistent Chrome profile is logged out.

Uses LINKEDIN_* / NAUKRI_* / INDEED_* from settings. Never logs passwords.
CAPTCHA, OTP, and identity checkpoints are not solved — those stay manual_review.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from config.settings import get_settings
from utils.logger import get_logger

logger = get_logger("jobapplypro.site_login")

# Sites we can sign into with email+password from .env
SUPPORTED_SITES = frozenset({"linkedin", "naukri", "indeed"})

_LINKEDIN_USER_SELECTORS = (
    "input#username",
    "input[name='session_key']",
    "input[autocomplete='username']",
    "input[type='text'][name='email']",
)
_LINKEDIN_PASS_SELECTORS = (
    "input#password",
    "input[name='session_password']",
    "input[autocomplete='current-password']",
    "input[type='password']",
)
_LINKEDIN_SUBMIT_SELECTORS = (
    "button[type='submit']",
    "button:has-text('Sign in')",
    "button[data-litms-control-urn*='login-submit']",
)

_NAUKRI_USER_SELECTORS = (
    "input#usernameField",
    "input[placeholder*='Email ID' i]",
    "input[placeholder*='Email' i]",
    "input[type='text'][name='email']",
    "input[name='username']",
)
_NAUKRI_PASS_SELECTORS = (
    "input#passwordField",
    "input[type='password']",
)
_NAUKRI_SUBMIT_SELECTORS = (
    "button.loginButton",
    "button:has-text('Login')",
    "button[type='submit']",
    "button:has-text('Sign in')",
)
_NAUKRI_OPEN_LOGIN_SELECTORS = (
    "a#login_Layer",
    "a:has-text('Login')",
    "button:has-text('Login')",
    ".nI-gNb-lg-rg__login",
)

_INDEED_USER_SELECTORS = (
    "input#ifl-InputFormField-3",
    "input[type='email']",
    "input[name='__email']",
    "input#login-email-input",
)
_INDEED_PASS_SELECTORS = (
    "input[type='password']",
    "input#login-password-input",
)
_INDEED_SUBMIT_SELECTORS = (
    "button[type='submit']",
    "button:has-text('Sign in')",
    "button:has-text('Continue')",
)

_CHALLENGE_URL_PARTS = (
    "/checkpoint",
    "/challenge",
    "/check/add",
    "security-verification",
    "challenge-page",
    "captcha",
)
_CHALLENGE_HTML = (
    "enter the code",
    "enter otp",
    "verification code",
    "we sent a code",
    "confirm it’s you",
    "confirm it's you",
    "let’s do a quick security check",
    "let's do a quick security check",
    "suspicious activity",
    "verify your identity",
    "resend otp",
)


@dataclass
class LoginAttempt:
    ok: bool
    site: str = ""
    reason: str = ""  # not_needed | not_configured | signed_in | failed | captcha | checkpoint
    captcha: bool = False
    checkpoint: bool = False


def site_from_url(url: str) -> str | None:
    host = (urlparse(url or "").hostname or "").lower()
    if "linkedin.com" in host:
        return "linkedin"
    if "naukri.com" in host:
        return "naukri"
    if "indeed.com" in host:
        return "indeed"
    return None


def credentials_for(site: str, settings: Any | None = None) -> tuple[str, str] | None:
    """Return (email, password) or None when either is missing."""
    cfg = settings or get_settings()
    mapping = {
        "linkedin": ("linkedin_email", "linkedin_password"),
        "naukri": ("naukri_email", "naukri_password"),
        "indeed": ("indeed_email", "indeed_password"),
    }
    keys = mapping.get(site)
    if not keys:
        return None
    email = str(getattr(cfg, keys[0], "") or "").strip()
    password = str(getattr(cfg, keys[1], "") or "")
    if not email or not password.strip():
        return None
    return email, password


def _safe_url(page) -> str:
    try:
        url = page.url
    except Exception:
        return ""
    return url if isinstance(url, str) else ""


def _safe_html(page) -> str:
    try:
        html = page.content()
    except Exception:
        return ""
    return html if isinstance(html, str) else ""


def _looks_like_challenge(url: str, html: str) -> bool:
    blob = f"{url}\n{html}".lower()
    if any(part in (url or "").lower() for part in _CHALLENGE_URL_PARTS):
        return True
    return any(token in blob for token in _CHALLENGE_HTML)


def _first_visible(page, selectors: tuple[str, ...], *, timeout_ms: int = 2500):
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                continue
            if loc.is_visible(timeout=timeout_ms):
                return loc
        except Exception:
            continue
    return None


def _fill_value(locator, value: str) -> None:
    locator.click(timeout=4000)
    try:
        locator.fill("")
    except Exception:
        pass
    try:
        locator.type(value, delay=35)
    except Exception:
        locator.fill(value)


def _click_first(page, selectors: tuple[str, ...]) -> bool:
    loc = _first_visible(page, selectors, timeout_ms=2000)
    if loc is None:
        return False
    try:
        loc.click(timeout=5000)
        return True
    except Exception:
        return False


def _wait(page, ms: int) -> None:
    try:
        page.wait_for_timeout(ms)
    except Exception:
        time.sleep(ms / 1000.0)


def _goto(page, url: str) -> None:
    if not url:
        return
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    _wait(page, 800)


def linkedin_needs_login(html: str, url: str) -> bool:
    from automation.site_adapters.linkedin_easy_apply import looks_like_login_wall

    return looks_like_login_wall(html, url)


def naukri_needs_login(html: str, url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    path = (urlparse(url or "").path or "").lower()
    if host.endswith("naukri.com") and any(
        token in path for token in ("/nlogin", "/mnjlogin", "/login")
    ):
        return True
    lower = (html or "").lower()
    if "usernamefield" in lower or 'id="usernamefield"' in lower:
        return True
    if "login to apply" in lower and "naukri" in (host or lower):
        return True
    return False


def indeed_needs_login(html: str, url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if not host.endswith("indeed.com"):
        return False
    if "bot-detection" in query:
        # Bot interstitial — password form usually is not the problem.
        return "auth" in path
    if "/auth" in path or "/account/login" in path:
        return True
    lower = (html or "").lower()
    return "sign in" in lower and ("indeed" in lower or host.endswith("indeed.com"))


def needs_login(site: str, html: str, url: str) -> bool:
    if site == "linkedin":
        return linkedin_needs_login(html, url)
    if site == "naukri":
        return naukri_needs_login(html, url)
    if site == "indeed":
        return indeed_needs_login(html, url)
    return False


def _linkedin_logged_in(html: str, url: str) -> bool:
    from automation.site_adapters.linkedin_easy_apply import looks_like_logged_in

    return looks_like_logged_in(html, url)


def _naukri_logged_in(html: str, url: str) -> bool:
    path = (urlparse(url or "").path or "").lower()
    if any(token in path for token in ("/nlogin", "/mnjlogin")):
        return False
    lower = (html or "").lower()
    if "usernamefield" in lower:
        return False
    return any(
        token in lower
        for token in (
            "ni-gnb-drawer",
            "mnjuser",
            "my naukri",
            "ni-gnb-info__subtxt",
        )
    )


def _indeed_logged_in(html: str, url: str) -> bool:
    path = (urlparse(url or "").path or "").lower()
    if "/auth" in path:
        return False
    lower = (html or "").lower()
    return "sign out" in lower or 'data-gnav-element-name="AccountMenu"' in html


def is_logged_in(site: str, html: str, url: str) -> bool:
    if site == "linkedin":
        return _linkedin_logged_in(html, url)
    if site == "naukri":
        return _naukri_logged_in(html, url)
    if site == "indeed":
        return _indeed_logged_in(html, url)
    return False


def _open_naukri_login_form(page) -> None:
    if _first_visible(page, _NAUKRI_USER_SELECTORS, timeout_ms=1200) is not None:
        return
    _click_first(page, _NAUKRI_OPEN_LOGIN_SELECTORS)
    _wait(page, 800)


def _submit_login(
    page,
    *,
    site: str,
    user_selectors: tuple[str, ...],
    pass_selectors: tuple[str, ...],
    submit_selectors: tuple[str, ...],
    email: str,
    password: str,
) -> LoginAttempt:
    user = _first_visible(page, user_selectors, timeout_ms=8000)
    if user is None:
        logger.warning("%s login: email field not found", site)
        return LoginAttempt(ok=False, site=site, reason="failed")
    _fill_value(user, email)

    # Indeed (and some LinkedIn flows) ask for email first, then password.
    if _first_visible(page, pass_selectors, timeout_ms=1500) is None:
        _click_first(page, submit_selectors)
        _wait(page, 1200)

    pw = _first_visible(page, pass_selectors, timeout_ms=8000)
    if pw is None:
        html, url = _safe_html(page), _safe_url(page)
        if _looks_like_challenge(url, html):
            return LoginAttempt(
                ok=False, site=site, reason="checkpoint", checkpoint=True
            )
        logger.warning("%s login: password field not found", site)
        return LoginAttempt(ok=False, site=site, reason="failed")
    _fill_value(pw, password)

    if not _click_first(page, submit_selectors):
        try:
            pw.press("Enter")
        except Exception:
            logger.warning("%s login: could not submit the form", site)
            return LoginAttempt(ok=False, site=site, reason="failed")

    _wait(page, 2000)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass
    _wait(page, 800)
    return LoginAttempt(ok=True, site=site, reason="submitted")


def try_credential_login(
    page,
    *,
    site: str | None = None,
    return_url: str | None = None,
    settings: Any | None = None,
    force: bool = False,
) -> LoginAttempt:
    """
    Sign in with .env credentials when the current page is a login wall.

    Returns ok=True when the session looks logged in (already or after sign-in).
    Pass force=True when the caller already detected a login wall (avoids
    consuming a second page.content() that tests mock with side_effect).
    """
    cfg = settings or get_settings()
    url = _safe_url(page) or (return_url or "")
    site = site or site_from_url(url)
    if site not in SUPPORTED_SITES:
        return LoginAttempt(ok=True, site=site or "", reason="not_needed")

    if not force:
        html = _safe_html(page)
        if is_logged_in(site, html, url) or not needs_login(site, html, url):
            return LoginAttempt(ok=True, site=site, reason="not_needed")

    creds = credentials_for(site, cfg)
    if creds is None:
        logger.info(
            "%s login wall with no %s_EMAIL/%s_PASSWORD in settings — skipping auto sign-in",
            site,
            site.upper(),
            site.upper(),
        )
        return LoginAttempt(ok=False, site=site, reason="not_configured")

    email, password = creds
    logger.info("Signing in to %s as %s (password not logged)", site, email)

    try:
        if site == "linkedin":
            if "/login" not in (url or "").lower() and "authwall" not in (url or "").lower():
                _goto(page, "https://www.linkedin.com/login")
            attempt = _submit_login(
                page,
                site=site,
                user_selectors=_LINKEDIN_USER_SELECTORS,
                pass_selectors=_LINKEDIN_PASS_SELECTORS,
                submit_selectors=_LINKEDIN_SUBMIT_SELECTORS,
                email=email,
                password=password,
            )
        elif site == "naukri":
            if "/nlogin" not in (url or "").lower():
                _open_naukri_login_form(page)
                if _first_visible(page, _NAUKRI_USER_SELECTORS, timeout_ms=1500) is None:
                    _goto(page, "https://www.naukri.com/nlogin/login")
            attempt = _submit_login(
                page,
                site=site,
                user_selectors=_NAUKRI_USER_SELECTORS,
                pass_selectors=_NAUKRI_PASS_SELECTORS,
                submit_selectors=_NAUKRI_SUBMIT_SELECTORS,
                email=email,
                password=password,
            )
        else:
            if "/auth" not in (url or "").lower():
                _goto(page, "https://secure.indeed.com/auth")
            attempt = _submit_login(
                page,
                site=site,
                user_selectors=_INDEED_USER_SELECTORS,
                pass_selectors=_INDEED_PASS_SELECTORS,
                submit_selectors=_INDEED_SUBMIT_SELECTORS,
                email=email,
                password=password,
            )
    except Exception as exc:
        logger.warning("%s auto sign-in failed: %s", site, exc)
        return LoginAttempt(ok=False, site=site, reason="failed")

    if not attempt.ok:
        return attempt

    html, url_now = _safe_html(page), _safe_url(page)
    if _looks_like_challenge(url_now, html):
        logger.warning("%s sign-in hit a verification challenge — not solving it", site)
        return LoginAttempt(ok=False, site=site, reason="checkpoint", checkpoint=True)

    if return_url:
        try:
            _goto(page, return_url)
        except Exception as exc:
            logger.warning("Could not return to job URL after %s login: %s", site, exc)

    html, url_now = _safe_html(page), _safe_url(page)
    if _looks_like_challenge(url_now, html):
        return LoginAttempt(ok=False, site=site, reason="checkpoint", checkpoint=True)
    if is_logged_in(site, html, url_now) or not needs_login(site, html, url_now):
        logger.info("%s session is ready after sign-in", site)
        return LoginAttempt(ok=True, site=site, reason="signed_in")

    logger.warning("%s auto sign-in did not clear the login wall", site)
    return LoginAttempt(ok=False, site=site, reason="failed")
