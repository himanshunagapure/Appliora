"""Lightweight browser helper tests (no Chromium launch)."""

from unittest.mock import MagicMock

from automation.browser import BrowserSession, html_looks_like_captcha


def test_html_looks_like_captcha_positive():
    assert html_looks_like_captcha("<div class='g-recaptcha'></div>")
    assert html_looks_like_captcha("Please verify you are a human")
    assert html_looks_like_captcha(
        '<iframe src="https://challenges.cloudflare.com/turnstile"></iframe>'
    )


def test_html_looks_like_captcha_negative():
    assert not html_looks_like_captcha("<form><input name='email'></form>")
    # Bare word alone should NOT trip (too many false positives on normal pages).
    assert not html_looks_like_captcha(
        "<p>We do not use captcha on this career site.</p>"
    )


def test_content_retries_while_page_navigating():
    """LinkedIn SPA often raises mid-navigation; content() should retry."""
    session = BrowserSession.__new__(BrowserSession)
    page = MagicMock()
    calls = {"n": 0}

    def flaky_content():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception(
                "Page.content: Unable to retrieve content because the page "
                "is navigating and changing the content."
            )
        return "<html>ok</html>"

    page.content.side_effect = flaky_content
    page.wait_for_timeout.return_value = None
    page.wait_for_load_state.return_value = None
    session._page = page

    html = session.content(retries=5, retry_delay_ms=1)
    assert html == "<html>ok</html>"
    assert calls["n"] == 3
