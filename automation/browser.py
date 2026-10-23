"""
Playwright browser session helpers for Phase 7 (FR6).

Dry-run fills never click final Submit. CAPTCHA is detected only — never solved.
Permissions (camera/mic/geolocation) are denied — matching ApplyPilot hard rules.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from config.settings import get_settings

# Close to a current Chrome UA so Workday/Akamai is less likely to treat login as a bot.
_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
from utils.logger import get_logger

logger = get_logger("jobapplypro.browser")

_playwright_mod = None

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
"""


def _playwright():
    global _playwright_mod
    if _playwright_mod is None:
        from playwright.sync_api import sync_playwright

        _playwright_mod = sync_playwright
    return _playwright_mod


def _install_stealth(context, page) -> None:
    """Reduce obvious Playwright fingerprints (Workday/Akamai reject webdriver)."""
    try:
        context.add_init_script(_STEALTH_JS)
    except Exception:
        pass
    try:
        page.add_init_script(_STEALTH_JS)
    except Exception:
        pass


# Prefer widget / challenge markers. Bare "captcha" alone is too noisy.
CAPTCHA_DEFAULT_SIGNALS = (
    "g-recaptcha",
    "h-captcha",
    "hcaptcha.com",
    "cf-turnstile",
    "challenges.cloudflare.com",
    "recaptcha/api",
    "funcaptcha",
    "arkoselabs",
    "are you a human",
    "verify you are a human",
    "verify you're human",
    "attention required",
    "challenge-platform",
    "cf-browser-verification",
    "unusual traffic",
)


def html_looks_like_captcha(html: str, signals: list[str] | None = None) -> bool:
    """Heuristic CAPTCHA / bot-wall detection from page HTML (no solving)."""
    if not isinstance(html, str):
        return False
    lower = html.lower()
    if not lower:
        return False
    for signal in signals or CAPTCHA_DEFAULT_SIGNALS:
        if signal.lower() in lower:
            return True
    if re.search(
        r"<iframe[^>]+src=[\"'][^\"']*(recaptcha|hcaptcha|turnstile|arkoselabs|funcaptcha)",
        lower,
        re.IGNORECASE,
    ):
        return True
    return False


class BrowserSession:
    """
    Context-managed Playwright Chromium session.

    Usage:
        with BrowserSession(headless=True) as browser:
            browser.goto(url)
            browser.screenshot(path)
    """

    def __init__(
        self,
        *,
        headless: bool | None = None,
        user_data_dir: str | Path | None = None,
        storage_state: str | Path | None = None,
        slow_mo_ms: int = 0,
    ):
        settings = get_settings()
        self.headless = (
            settings.browser_headless if headless is None else headless
        )
        raw_udir = (
            user_data_dir
            if user_data_dir is not None
            else settings.browser_user_data_dir
        )
        self.user_data_dir = Path(raw_udir) if raw_udir else None
        self.storage_state = Path(storage_state) if storage_state else None
        self.slow_mo_ms = slow_mo_ms
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self) -> "BrowserSession":
        settings = get_settings()
        self._pw = _playwright()().start()
        launch_args: dict[str, Any] = {
            "headless": self.headless,
            "slow_mo": self.slow_mo_ms,
            # Hide Playwright's default --enable-automation / infobar.
            "ignore_default_args": ["--enable-automation"],
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--deny-permission-prompts",
            ],
        }
        channel = (settings.browser_channel or "").strip()
        if channel:
            launch_args["channel"] = channel
        context_kwargs: dict[str, Any] = {
            "accept_downloads": True,
            "viewport": {"width": 1280, "height": 900},
            # Never grant camera / mic / geolocation (ApplyPilot hard rule).
            "permissions": [],
            "geolocation": None,
            "locale": "en-US",
            "user_agent": _DEFAULT_UA,
        }

        if self.user_data_dir:
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            self._context = self._launch_persistent(launch_args, context_kwargs)
            self._browser = None
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )
        else:
            if self.storage_state and self.storage_state.is_file():
                context_kwargs["storage_state"] = str(self.storage_state)
                logger.info("Loading browser storage_state from %s", self.storage_state)
            self._browser = self._pw.chromium.launch(**launch_args)
            self._context = self._browser.new_context(**context_kwargs)
            self._page = self._context.new_page()

        _install_stealth(self._context, self._page)

        try:
            self._context.clear_permissions()
        except Exception:
            pass

        # Auto-dismiss JS dialogs; never accept permission-like prompts.
        def _on_dialog(dialog) -> None:
            try:
                dialog.dismiss()
            except Exception:
                pass

        self._page.on("dialog", _on_dialog)

        logger.info(
            "BrowserSession started (headless=%s, persistent=%s)",
            self.headless,
            bool(self.user_data_dir),
        )
        return self

    def _launch_persistent(self, launch_args: dict[str, Any], context_kwargs: dict[str, Any]):
        assert self.user_data_dir is not None
        try:
            return self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                **launch_args,
                **context_kwargs,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "already in use" not in msg and "existing browser session" not in msg:
                raise
            logger.warning("Persistent profile in use — closing leftover Chrome and retrying")
            free_persistent_profile(self.user_data_dir)
            time.sleep(1.2)
            return self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                **launch_args,
                **context_kwargs,
            )

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._context is not None:
                self._context.close()
            elif self._browser is not None:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._page = None
            self._context = None
            self._browser = None
            self._pw = None
            logger.info("BrowserSession closed")

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("BrowserSession is not started")
        return self._page

    def adopt_page(self, page) -> None:
        """Switch the session to a popup (Indeed Apply often opens a new tab)."""
        self._page = page
        try:
            self._page.on("dialog", lambda dialog: dialog.dismiss())
        except Exception:
            pass

    @property
    def current_url(self) -> str:
        try:
            return self.page.url or ""
        except Exception:
            return ""

    def goto(self, url: str, *, wait_until: str = "domcontentloaded", timeout_ms: int = 60000):
        """
        Navigate and wait for the document to settle.

        LinkedIn (and similar SPAs) often keep navigating after the first
        domcontentloaded — calling content() too early raises:
        "Unable to retrieve content because the page is navigating..."
        """
        logger.info("Navigating to %s", url)
        self.page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        # LinkedIn never reaches true networkidle (long-poll). Keep this short.
        self.wait_until_settled(timeout_ms=8000, quiet_ms=400, networkidle_ms=1500)

    def wait_until_settled(
        self,
        *,
        timeout_ms: int = 8000,
        quiet_ms: int = 400,
        networkidle_ms: int = 1500,
    ) -> None:
        """Wait for load + a short quiet period so SPA redirects can finish."""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            logger.debug("wait_for_load_state(domcontentloaded): %s", exc)
        if networkidle_ms > 0:
            try:
                self.page.wait_for_load_state(
                    "networkidle", timeout=min(timeout_ms, networkidle_ms)
                )
            except Exception:
                pass
        try:
            self.page.wait_for_timeout(quiet_ms)
        except Exception:
            pass

    def content(self, *, retries: int = 5, retry_delay_ms: int = 500) -> str:
        """Read page HTML, retrying while the SPA is still navigating."""
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return self.page.content()
            except Exception as exc:
                last_error = exc
                msg = str(exc).lower()
                navigating = "navigating" in msg or "changing the content" in msg
                if not navigating or attempt >= retries:
                    break
                logger.debug(
                    "page.content() not ready (attempt %d/%d): %s",
                    attempt,
                    retries,
                    exc,
                )
                try:
                    self.page.wait_for_timeout(retry_delay_ms)
                except Exception:
                    pass
                self.wait_until_settled(timeout_ms=5000, quiet_ms=200)
        assert last_error is not None
        raise last_error

    def detect_captcha(self, signals: list[str] | None = None) -> bool:
        try:
            html = self.content()
        except Exception as exc:
            logger.warning("Could not read page content for captcha check: %s", exc)
            return False
        hit = html_looks_like_captcha(html, signals=signals)
        if hit:
            logger.warning("CAPTCHA / bot wall signals detected on page")
        return hit

    def screenshot(self, path: Path | str, *, full_page: bool = True) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Screenshots can also fail mid-navigation — settle first.
        self.wait_until_settled(timeout_ms=5000, quiet_ms=200)
        self.page.screenshot(path=str(path), full_page=full_page)
        logger.info("Screenshot saved to %s", path)
        return path

    def save_storage_state(self, path: Path | str) -> Path:
        """
        Persist cookies + localStorage so a later submit session can reuse
        login / ATS draft state from the dry-run (new browser process).
        """
        if self._context is None:
            raise RuntimeError("BrowserSession is not started")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(path))
        logger.info("Saved browser storage_state to %s", path)
        return path

    def click(self, selector: str, *, timeout_ms: int = 10000) -> None:
        self.page.click(selector, timeout=timeout_ms)

    def fill(self, selector: str, value: str, *, timeout_ms: int = 10000) -> None:
        self.page.fill(selector, value, timeout=timeout_ms)

    def upload_file(self, selector: str, file_path: Path | str) -> None:
        self.page.set_input_files(selector, str(file_path))

    def select_option(self, selector: str, value: str) -> None:
        self.page.select_option(selector, value)

    def query_all(self, selector: str):
        return self.page.query_selector_all(selector)


def free_persistent_profile(user_data_dir: Path) -> None:
    """Stop a leftover Chromium that still holds BROWSER_USER_DATA_DIR."""
    needle = f"user-data-dir={user_data_dir}"
    try:
        out = subprocess.check_output(["pgrep", "-af", needle], text=True)
    except Exception:
        out = ""
    for line in out.splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        cmd = parts[1].lower() if len(parts) > 1 else ""
        if "pgrep" in cmd:
            continue
        if not any(name in cmd for name in ("chrome", "chromium")):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    time.sleep(0.6)
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (user_data_dir / name).unlink(missing_ok=True)
        except Exception:
            pass
