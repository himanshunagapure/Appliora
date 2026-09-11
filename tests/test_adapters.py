"""Adapter unit tests with a fake Playwright-like page (no Chromium)."""

from pathlib import Path
from unittest.mock import MagicMock

from automation.site_adapters.generic import GenericAdapter
from automation.site_adapters.linkedin_easy_apply import LinkedInEasyApplyAdapter
from automation.site_adapters.workday import WorkdayAdapter
from automation.types import FieldMapping, FillResult, FormPlan

PROFILE = {
    "contact": {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "phone": "555-0100",
        "location": "Nagpur, India",
    }
}


def _page_with_html(html: str = "") -> MagicMock:
    page = MagicMock()
    page.content.return_value = html
    page.locator.return_value.count.return_value = 0
    return page


def test_generic_fill_dry_run_skips_submit(tmp_path):
    resume = tmp_path / "resume.md"
    resume.write_text("# Jane\n")
    page = _page_with_html()
    plan = FormPlan(
        ats="generic",
        fields=[
            FieldMapping("email", "#email", "text", True, 0.9),
            FieldMapping("full_name", "#full_name", "text", True, 0.9),
            FieldMapping("resume", "#resume", "file", True, 0.9),
        ],
        submit_button="button:has-text('Submit application')",
        confidence=0.9,
    )
    result = GenericAdapter().fill(
        page,
        plan,
        PROFILE,
        {"resume_path": str(resume), "cover_letter_text": "Hello"},
        dry_run=True,
    )
    assert result.status == "filled"
    assert "email" in result.fields_filled
    assert "full_name" in result.fields_filled
    # dry-run must not click submit
    for call in page.click.call_args_list:
        assert "Submit" not in str(call)


def test_generic_fill_submit_clicks_when_not_dry_run():
    page = _page_with_html()
    plan = FormPlan(
        ats="generic",
        fields=[FieldMapping("email", "#email", "text", True, 0.9)],
        submit_button="#submit",
        confidence=0.9,
    )
    result = GenericAdapter().fill(
        page, plan, PROFILE, {}, dry_run=False
    )
    assert result.status == "submitted"
    page.click.assert_any_call("#submit")


def test_linkedin_detects_logged_in_feed():
    from automation.site_adapters.linkedin_easy_apply import (
        looks_like_logged_in,
        looks_like_login_wall,
        session_looks_ready,
    )

    feed_html = """
    <div id="global-nav" class="global-nav">
      <button class="global-nav__me" aria-label="Me"></button>
      <div class="feed-identity-module">Himanshu</div>
      <div class="notification-badge">24</div>
    </div>
    <p>Some footer may mention sign in for other people</p>
    """
    url = "https://www.linkedin.com/feed/"
    assert looks_like_logged_in(feed_html, url) is True
    assert looks_like_login_wall(feed_html, url) is False
    assert session_looks_ready(feed_html, url) is True


def test_linkedin_matches_and_manual_review_without_easy_apply(monkeypatch):
    class _Settings:
        browser_headless = True
        browser_login_wait_seconds = 5

    monkeypatch.setattr(
        "automation.site_adapters.linkedin_easy_apply.get_settings",
        lambda: _Settings(),
    )
    adapter = LinkedInEasyApplyAdapter()
    assert adapter.matches("https://www.linkedin.com/jobs/view/1", "")
    page = _page_with_html("<html>Sign in to see who you already know at Acme</html>")
    page.click.side_effect = Exception("no button")
    result = adapter.fill(
        page,
        FormPlan(ats="linkedin_easy_apply", confidence=0.7),
        PROFILE,
        {},
        dry_run=True,
    )
    assert result.status == "manual_review"
    assert "login_required" in (result.error or "")


def test_linkedin_headed_waits_for_login_then_continues(monkeypatch):
    class _Settings:
        browser_headless = False
        browser_login_wait_seconds = 30

    monkeypatch.setattr(
        "automation.site_adapters.linkedin_easy_apply.get_settings",
        lambda: _Settings(),
    )

    calls = {"n": 0}

    def fake_wait(page, *, timeout_sec, job_url=None, poll_sec=2.0):
        calls["n"] += 1
        return True

    monkeypatch.setattr(
        "automation.site_adapters.linkedin_easy_apply.wait_for_manual_login",
        fake_wait,
    )
    monkeypatch.setattr(
        "automation.site_adapters.linkedin_easy_apply._click_apply",
        lambda page: (page, "easy"),
    )
    monkeypatch.setattr(
        "automation.site_adapters.linkedin_easy_apply._wait_for_apply_button",
        lambda page, timeout_ms=12000: None,
    )
    monkeypatch.setattr(
        "automation.site_adapters.linkedin_easy_apply._fill_easy_apply_modal",
        lambda *a, **k: FillResult(status="filled", adapter="linkedin_easy_apply"),
    )

    adapter = LinkedInEasyApplyAdapter()
    page = MagicMock()
    page.content.side_effect = [
        "<html>Sign in to see who you already know</html>",
        "<html><button class='jobs-apply-button'>Easy Apply</button></html>",
    ]
    page.url = "https://www.linkedin.com/jobs/view/1"

    result = adapter.fill(
        page,
        FormPlan(ats="linkedin_easy_apply", confidence=0.8),
        PROFILE,
        {"job_url": "https://www.linkedin.com/jobs/view/1"},
        dry_run=True,
    )
    assert calls["n"] == 1
    assert result.status == "filled"


def test_linkedin_dry_run_stops_before_submit(monkeypatch):
    class _Settings:
        browser_headless = True
        browser_login_wait_seconds = 5

    monkeypatch.setattr(
        "automation.site_adapters.linkedin_easy_apply.get_settings",
        lambda: _Settings(),
    )
    html = (Path(__file__).parent / "fixtures/forms/easy_apply_modal.html").read_text()
    adapter = LinkedInEasyApplyAdapter()
    page = MagicMock()
    page.content.return_value = html
    page.url = "https://www.linkedin.com/jobs/view/1"

    monkeypatch.setattr(
        "automation.site_adapters.linkedin_easy_apply._click_apply",
        lambda p: (page, "easy"),
    )
    monkeypatch.setattr(
        "automation.site_adapters.linkedin_easy_apply._wait_for_apply_button",
        lambda page, timeout_ms=12000: None,
    )

    # locator: Next has count 1 first, then Submit visible
    next_loc = MagicMock()
    next_loc.count.return_value = 1
    next_loc.first.inner_text.return_value = "Next"
    next_loc.first.click.return_value = None

    submit_loc = MagicMock()
    submit_loc.count.return_value = 1
    submit_loc.first.inner_text.return_value = "Submit application"

    def locator_side_effect(sel):
        if "Submit" in sel or "submit" in sel.lower():
            return submit_loc
        if "Next" in sel or "Continue" in sel or "Review" in sel:
            return next_loc
        return MagicMock(count=MagicMock(return_value=0))

    page.locator.side_effect = locator_side_effect

    result = adapter.fill(
        page,
        FormPlan(ats="linkedin_easy_apply", confidence=0.8),
        PROFILE,
        {},
        dry_run=True,
    )
    assert result.status == "filled"


def test_fill_easy_apply_phone_via_type_tel_and_label():
    """New LinkedIn UI uses input[type=tel] + label, not name='phoneNumber'."""
    from automation.site_adapters.linkedin_easy_apply import (
        _fill_easy_apply_contact_fields,
        _normalize_phone,
    )

    assert _normalize_phone("866-844-0121") == "8668440121"
    assert _normalize_phone("+91 86684 40121") == "+918668440121"

    page = MagicMock()
    phone_loc = MagicMock()
    phone_loc.count.return_value = 1
    phone_loc.first.is_visible.return_value = True
    phone_loc.first.input_value.return_value = "8668440121"

    empty_loc = MagicMock()
    empty_loc.count.return_value = 0

    def get_by_label(name, exact=False):
        if "phone" in name.lower() or "mobile" in name.lower():
            return phone_loc
        return empty_loc

    page.get_by_label.side_effect = get_by_label
    page.locator.return_value.count.return_value = 0

    filled, skipped = _fill_easy_apply_contact_fields(
        page,
        {
            "phone": "866-844-0121",
            "email": "jane@example.com",
            "first_name": "Jane",
            "last_name": "Doe",
        },
    )
    assert "phone" in filled
    phone_loc.first.fill.assert_any_call("8668440121", timeout=2500)


def test_linkedin_external_apply_stays_on_linkedin_is_manual_review(monkeypatch):
    class _Settings:
        browser_headless = True
        browser_login_wait_seconds = 5

    monkeypatch.setattr(
        "automation.site_adapters.linkedin_easy_apply.get_settings",
        lambda: _Settings(),
    )
    page = MagicMock()
    page.content.return_value = (
        "<html>Responses managed off LinkedIn "
        "<button class='jobs-apply-button'>Apply</button></html>"
    )
    page.url = "https://www.linkedin.com/jobs/view/1"
    monkeypatch.setattr(
        "automation.site_adapters.linkedin_easy_apply._wait_for_apply_button",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "automation.site_adapters.linkedin_easy_apply._click_apply",
        lambda p: (page, "external"),
    )
    result = LinkedInEasyApplyAdapter().fill(
        page,
        FormPlan(ats="linkedin_easy_apply", confidence=0.8),
        PROFILE,
        {},
        dry_run=True,
    )
    assert result.status == "manual_review"
    assert "off-LinkedIn" in (result.error or "") or "external" in (result.error or "")


def test_workday_matches_myworkday_host():
    adapter = WorkdayAdapter()
    assert adapter.matches(
        "https://acme.myworkdayjobs.com/en-US/careers/job/1", ""
    )


def test_greenhouse_matches_gh_jid_company_page():
    from automation.site_adapters.greenhouse import GreenhouseAdapter

    adapter = GreenhouseAdapter()
    assert adapter.matches(
        "https://www.precisely.com/careers-and-culture/us-jobs/job/?gh_jid=4710645005",
        "",
    )
    assert adapter.matches("https://boards.greenhouse.io/acme/jobs/1", "")


def test_greenhouse_fill_uses_iframe_fields(tmp_path):
    from automation.site_adapters.greenhouse import GreenhouseAdapter

    resume = tmp_path / "r.md"
    resume.write_text("resume")
    cl = tmp_path / "cl.md"
    cl.write_text("cover")

    page = MagicMock()
    page.url = "https://www.precisely.com/careers/job/?gh_jid=1"
    page.frames = []
    page.locator.return_value.count.return_value = 0

    frame = MagicMock()
    frame.url = "https://job-boards.greenhouse.io/embed/job_app?for=acme"
    first = MagicMock()
    first.count.return_value = 1

    def frame_locator(sel):
        loc = MagicMock()
        if sel in (
            "input#first_name",
            "input#last_name",
            "input#email",
            "input#phone",
            "input#resume",
            "input#cover_letter",
        ):
            loc.count.return_value = 1
            return loc
        loc.count.return_value = 0
        return loc

    frame.locator.side_effect = frame_locator
    page.frames = [frame]

    # dismiss_cookie_banners + wait helpers call locator/wait on page
    page.wait_for_timeout = MagicMock()

    result = GreenhouseAdapter().fill(
        page,
        FormPlan(ats="greenhouse", confidence=0.8),
        PROFILE,
        {"resume_path": str(resume), "cover_letter_path": str(cl)},
        dry_run=True,
    )
    assert result.status == "filled"
    assert "first_name" in result.fields_filled
    assert "email" in result.fields_filled
    assert "resume" in result.fields_filled
    assert result.adapter == "greenhouse"


def test_dismiss_cookie_banners_clicks_onetrust_accept():
    from automation.cookies import dismiss_cookie_banners

    page = MagicMock()
    accept = MagicMock()
    accept.count.return_value = 1
    accept.is_visible.return_value = True

    def locator(sel):
        loc = MagicMock()
        if sel == "#onetrust-accept-btn-handler":
            loc.first = accept
            loc.count.return_value = 1
            return loc
        loc.first.count.return_value = 0
        loc.count.return_value = 0
        return loc

    page.locator.side_effect = locator
    page.wait_for_timeout = MagicMock()
    assert dismiss_cookie_banners(page) is True
    accept.click.assert_called()


def test_workday_dry_run_fills_and_stops_on_submit_visible(tmp_path):
    resume = tmp_path / "r.md"
    resume.write_text("resume")
    adapter = WorkdayAdapter()
    page = MagicMock()
    page.content.return_value = "<html data-automation-id='x'>workday</html>"

    submit_loc = MagicMock()
    submit_loc.count.return_value = 1
    submit_loc.first.inner_text.return_value = "Submit"

    next_loc = MagicMock()
    next_loc.count.return_value = 0

    def locator_side_effect(sel):
        if "Submit" in sel or "submit" in sel:
            return submit_loc
        return next_loc

    page.locator.side_effect = locator_side_effect

    result = adapter.fill(
        page,
        FormPlan(ats="workday", confidence=0.8, multi_page=True),
        PROFILE,
        {"resume_path": str(resume)},
        dry_run=True,
    )
    assert result.status == "filled"


def test_workday_answers_how_heard_and_previous_worker():
    from automation.site_adapters.workday import _fill_workday_page

    filled: list[str] = []
    page = MagicMock()
    page.content.return_value = "<html></html>"

    how = MagicMock()
    how.count.return_value = 1
    how.first.click = MagicMock()

    prev_q = MagicMock()
    prev_q.count.return_value = 1
    no_radio = MagicMock()
    no_radio.count.return_value = 1
    prev_q.first.locator.return_value.get_by_text.return_value = no_radio

    empty = MagicMock()
    empty.count.return_value = 0

    def get_by_text(pat, **kwargs):
        text = pat.pattern if hasattr(pat, "pattern") else str(pat)
        if "How Did You Hear" in text:
            return how
        if "previously worked" in text:
            return prev_q
        return empty

    page.get_by_text.side_effect = get_by_text
    option = MagicMock()
    option.count.return_value = 1
    matched = MagicMock()
    matched.count.return_value = 1
    matched.first.click = MagicMock()
    option.filter.return_value = matched
    option.first.click = MagicMock()

    def locator(sel):
        if "promptOption" in sel or "option" in sel:
            return option
        loc = MagicMock()
        loc.count.return_value = 0
        return loc

    page.locator.side_effect = locator
    page.keyboard.type = MagicMock()
    page.keyboard.press = MagicMock()
    page.wait_for_timeout = MagicMock()
    page.evaluate.side_effect = Exception("no js")

    _fill_workday_page(
        page,
        {"how_heard": "LinkedIn", "previously_worked_here": "No"},
        PROFILE,
        {},
        filled,
    )
    assert "how_heard" in filled
    assert "previous_worker" in filled


def test_workday_start_modal_prefers_last_application_over_manual(tmp_path):
    from automation.site_adapters.workday import _handle_start_application_modal

    resume = tmp_path / "r.docx"
    resume.write_bytes(b"PK\x03\x04fake")

    page = MagicMock()
    manual = MagicMock()
    manual.count.return_value = 1
    manual.first.is_visible.return_value = True
    last_app = MagicMock()
    last_app.count.return_value = 1
    last_app.first.is_visible.return_value = True
    empty = MagicMock()
    empty.count.return_value = 0

    def locator(sel):
        if "Apply Manually" in sel:
            return manual
        if "Last Application" in sel:
            return last_app
        return empty

    page.locator.side_effect = locator
    started = _handle_start_application_modal(page, resume)
    assert started == "last_application"
    last_app.first.click.assert_called()
    manual.first.click.assert_not_called()


def test_workday_start_modal_prefers_apply_manually(tmp_path):
    from automation.site_adapters.workday import _handle_start_application_modal

    resume = tmp_path / "r.docx"
    resume.write_bytes(b"PK\x03\x04fake")

    page = MagicMock()
    manual = MagicMock()
    manual.count.return_value = 1
    manual.first.is_visible.return_value = True
    empty = MagicMock()
    empty.count.return_value = 0

    def locator(sel):
        if "Apply Manually" in sel:
            return manual
        return empty

    page.locator.side_effect = locator
    started = _handle_start_application_modal(page, resume)
    assert started == "manual"
    manual.first.click.assert_called()


def test_workday_start_modal_prefers_last_application(tmp_path):
    from automation.site_adapters.workday import _handle_start_application_modal

    resume = tmp_path / "r.docx"
    resume.write_bytes(b"PK\x03\x04fake")

    page = MagicMock()
    last_app = MagicMock()
    last_app.count.return_value = 1
    last_app.first.is_visible.return_value = True
    autofill = MagicMock()
    autofill.count.return_value = 1
    autofill.first.is_visible.return_value = True
    empty = MagicMock()
    empty.count.return_value = 0

    def locator(sel):
        if "Last Application" in sel:
            return last_app
        if "Autofill" in sel:
            return autofill
        return empty

    page.locator.side_effect = locator
    started = _handle_start_application_modal(page, resume)
    assert started == "last_application"
    last_app.first.click.assert_called()
    autofill.first.click.assert_not_called()


def test_workday_start_modal_clicks_autofill_with_resume(tmp_path):
    from automation.site_adapters.workday import _handle_start_application_modal

    resume = tmp_path / "r.docx"
    resume.write_bytes(b"PK\x03\x04fake")

    page = MagicMock()
    autofill = MagicMock()
    autofill.count.return_value = 1
    autofill.first.is_visible.return_value = True
    empty = MagicMock()
    empty.count.return_value = 0

    def locator(sel):
        if "Autofill" in sel:
            return autofill
        return empty

    page.locator.side_effect = locator
    chooser = MagicMock()
    page.expect_file_chooser.return_value.__enter__.return_value.value = chooser

    started = _handle_start_application_modal(page, resume)
    assert started == "resume"
    chooser.set_files.assert_called_with(str(resume))


def test_parse_workday_date_and_swap_inverted_range():
    from automation.site_adapters.workday import parse_workday_date, _ordered_dates

    present = parse_workday_date("Present")
    assert present["current"] is True
    may = parse_workday_date("May 2025")
    assert may == {"month": "May", "year": "2025", "current": False}
    mmy = parse_workday_date("05/2025")
    assert mmy["month"] == "May" and mmy["year"] == "2025"
    year_only = parse_workday_date("2022")
    assert year_only["year"] == "2022"
    start, end = _ordered_dates("Dec 2024", "Feb 2024")
    assert start["month"] == "February" and start["year"] == "2024"
    assert end["month"] == "December" and end["year"] == "2024"


def test_fill_work_experience_panel_uses_profile_job():
    from automation.site_adapters.workday import _fill_work_experience_panel

    page = MagicMock()
    title = MagicMock()
    title.count.return_value = 1
    title.first.is_visible.return_value = True
    company = MagicMock()
    company.count.return_value = 1
    company.first.is_visible.return_value = True
    empty = MagicMock()
    empty.count.return_value = 0
    checkbox = MagicMock()
    checkbox.count.return_value = 1

    def locator(sel):
        if "jobTitle" in sel:
            return title
        if "company" in sel:
            return company
        if "currentlyWorkHere" in sel:
            return checkbox
        return empty

    page.locator.side_effect = locator
    page.wait_for_timeout = MagicMock()
    page.keyboard.press = MagicMock()

    _fill_work_experience_panel(
        page,
        {
            "company": "AIQoD",
            "title": "AI/ML Developer (Generative AI)",
            "start_date": "May 2025",
            "end_date": "Present",
            "achievements": ["Built a LangGraph agent."],
        },
    )
    title.first.fill.assert_called()
    assert "AI/ML Developer" in title.first.fill.call_args[0][0]
    company.first.fill.assert_called()
    assert company.first.fill.call_args[0][0] == "AIQoD"
    checkbox.first.check.assert_called()


def test_on_my_experience_page_ignores_progress_bar_and_my_information():
    from automation.site_adapters.workday import _on_my_experience_page

    page = MagicMock()
    heard = MagicMock()
    heard.count.return_value = 1
    heard.first.is_visible.return_value = True
    empty = MagicMock()
    empty.count.return_value = 0

    page.get_by_text.return_value = heard
    page.get_by_role.return_value = empty
    page.locator.return_value = empty
    empty.first.is_visible.return_value = False

    assert _on_my_experience_page(page) is False


def test_upload_workday_resume_skips_hidden_file_input(tmp_path):
    from automation.site_adapters.workday import _upload_workday_resume

    resume = tmp_path / "r.docx"
    resume.write_bytes(b"PK\x03\x04fake")
    page = MagicMock()
    hidden = MagicMock()
    hidden.count.return_value = 1
    hidden.first.is_visible.return_value = False
    page.locator.return_value = hidden
    assert _upload_workday_resume(page, resume) is False
    hidden.first.set_input_files.assert_not_called()


def test_start_modal_is_not_account_wall_despite_header_sign_in():
    from automation.site_adapters.workday import _looks_like_workday_account_wall

    page = MagicMock()
    autofill = MagicMock()
    autofill.count.return_value = 1
    autofill.first.is_visible.return_value = True
    empty = MagicMock()
    empty.count.return_value = 0
    empty.first.is_visible.return_value = False
    sign_in = MagicMock()
    sign_in.count.return_value = 1
    sign_in.first.is_visible.return_value = True

    def locator(sel):
        if "Autofill with Resume" in sel:
            return autofill
        loc = MagicMock()
        loc.count.return_value = 0
        loc.first.is_visible.return_value = False
        return loc

    def get_by_text(pat, **kwargs):
        text = pat.pattern if hasattr(pat, "pattern") else str(pat)
        if "Sign In" in text:
            return sign_in
        if "Start Your Application" in text:
            loc = MagicMock()
            loc.count.return_value = 1
            loc.first.is_visible.return_value = True
            return loc
        return empty

    page.locator.side_effect = locator
    page.get_by_text.side_effect = get_by_text
    assert _looks_like_workday_account_wall(page) is False


def test_candidate_home_means_session_active_not_login_wall():
    from automation.site_adapters.workday import _looks_like_workday_account_wall

    page = MagicMock()
    home = MagicMock()
    home.count.return_value = 1
    home.first.is_visible.return_value = True
    empty = MagicMock()
    empty.count.return_value = 0
    empty.first.is_visible.return_value = False

    def locator(sel):
        if "Candidate Home" in sel:
            return home
        loc = MagicMock()
        loc.count.return_value = 0
        loc.first.is_visible.return_value = False
        return loc

    page.locator.side_effect = locator
    page.get_by_text.return_value = empty
    assert _looks_like_workday_account_wall(page) is False


def test_first_match_and_guess_select_answer():
    from automation.site_adapters.workday import _first_match, _guess_select_answer

    texts = ["Job Boards", "Socially", "Website"]
    assert _first_match(texts, ["LinkedIn", "Job Boards"]) == "Job Boards"
    opts = ["Yes", "No", "None"]
    assert _guess_select_answer("worked on a project with Accenture", opts, {}) == "None"
    long_opt = "I understand and I'm ready to continue with my application involving artificial intelligence"
    assert (
        _guess_select_answer("artificial intelligence screening", [long_opt, "Opt out"], {})
        == long_opt
    )
    assert (
        _guess_select_answer(
            "consent to recorded Workday interviews", ["Yes", "No"], {}
        )
        == "Yes"
    )


def test_autofill_page_ignored_when_experience_heading_visible():
    from automation.site_adapters.workday import _on_autofill_resume_page

    page = MagicMock()
    empty = MagicMock()
    empty.count.return_value = 0
    empty.first.is_visible.return_value = False
    visible = MagicMock()
    visible.count.return_value = 1
    visible.first.is_visible.return_value = True
    title = MagicMock()
    title.count.return_value = 1
    title.first.inner_text.return_value = "My Experience"
    title.first.is_visible.return_value = True

    def locator(sel, **kwargs):
        if "applyFlowPageTitle" in sel or "pageHeader" in sel:
            loc = MagicMock()
            loc.count.return_value = 1
            loc.first.inner_text.return_value = "My Experience"
            loc.first.is_visible.return_value = True
            loc.filter.return_value = loc
            return loc
        if "legalNameSection_firstName" in sel:
            return empty
        if "workExperienceSection" in sel:
            return visible
        return empty

    def get_by_text(pat, **kwargs):
        text = pat.pattern if hasattr(pat, "pattern") else str(pat)
        if "How Did You Hear" in text:
            return empty
        if "Drop file" in text or "Select file" in text:
            return visible
        if "Work Experience" in text:
            return visible
        return empty

    page.locator.side_effect = locator
    page.get_by_text.side_effect = get_by_text
    page.get_by_role.return_value = empty
    assert _on_autofill_resume_page(page) is False


def test_select_one_skips_error_banner_duplicate():
    from automation.site_adapters.workday import _guess_select_answer

    assert _guess_select_answer("Do you consent", ["Yes", "No"], {"_answer": "Yes"}) == "Yes"


def test_experience_jobs_for_workday_limits_to_two():
    from automation.site_adapters.workday import _experience_jobs_for_workday

    profile = {
        "experience": [
            {"company": "AIQoD", "title": "AI Engineer"},
            {"company": "Unified Mentor", "title": "Data Scientist"},
            {"company": "Other", "title": "X"},
        ]
    }
    jobs = _experience_jobs_for_workday(profile)
    assert len(jobs) == 2
    assert jobs[0]["company"] == "AIQoD"
    assert jobs[1]["company"] == "Unified Mentor"


def test_gpa_from_marks_converts_percentage():
    from automation.site_adapters.workday import _gpa_from_marks

    assert _gpa_from_marks("87%") == "8.7"
    assert _gpa_from_marks("9.46") == "9.46"


def test_primary_education_picks_bachelors():
    from automation.site_adapters.workday import _primary_education_for_workday

    profile = {
        "education": [
            {"degree": "HSC", "institution": "College A"},
            {"degree": "B.E.", "institution": "GCOEN"},
        ]
    }
    school = _primary_education_for_workday(profile)
    assert school["institution"] == "GCOEN"
