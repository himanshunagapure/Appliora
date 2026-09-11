"""Unit tests for form detection cascade (no live browser)."""

from pathlib import Path

from automation.form_detector import (
    build_form_plan,
    detect_ats,
    detect_captcha,
    detect_fields_from_html,
    extract_json_ld,
    is_manual_ats,
    profile_value_map,
)

FIXTURES = Path(__file__).parent / "fixtures" / "forms"


def test_detect_ats_from_url():
    assert detect_ats("https://www.linkedin.com/jobs/view/123") == "linkedin_easy_apply"
    assert (
        detect_ats("https://company.myworkdayjobs.com/en-US/careers/job/1") == "workday"
    )
    assert detect_ats("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert (
        detect_ats(
            "https://www.precisely.com/careers-and-culture/us-jobs/job/?gh_jid=4710645005"
        )
        == "greenhouse"
    )
    assert detect_ats("https://careers.example.com/apply") == "generic"
    assert detect_ats("https://jobs.lever.co/acme/1") == "lever"
    assert detect_ats("https://apply.workable.com/acme/j/1") == "workable"
    assert (
        detect_ats("https://acme.wd5.myworkdaysite.com/en-US/job/1") == "workday"
    )
    # Substring traps: clever.co is not lever.co
    assert detect_ats("https://clever.co/jobs/1") == "generic"
    assert (
        detect_ats(
            "https://careers.example.com/apply",
            html='<iframe src="https://jobs.lever.co/acme/1"></iframe>',
        )
        == "lever"
    )


def test_detect_captcha_on_fixture():
    html = (FIXTURES / "captcha_interstitial.html").read_text(encoding="utf-8")
    assert detect_captcha(html) is True


def test_detect_fields_from_generic_html():
    html = (FIXTURES / "generic_apply.html").read_text(encoding="utf-8")
    fields = detect_fields_from_html(html)
    names = {f.name for f in fields}
    assert "email" in names
    assert "full_name" in names
    assert "resume" in names
    assert "cover_letter" in names


def test_extract_json_ld_job_posting():
    html = (FIXTURES / "generic_apply.html").read_text(encoding="utf-8")
    blocks = extract_json_ld(html)
    assert blocks
    assert blocks[0].get("@type") == "JobPosting"


def test_build_form_plan_generic_high_confidence():
    html = (FIXTURES / "generic_apply.html").read_text(encoding="utf-8")
    plan = build_form_plan("https://careers.example.com/apply", html)
    assert plan.ats == "generic"
    assert plan.confidence >= 0.55
    assert plan.detection_tier >= 1
    assert plan.submit_button is not None or plan.apply_button is not None
    # "Apply" is a real button; "Submit application" must not be treated as Apply.
    assert plan.apply_button is None or "Submit" not in (plan.apply_button or "")
    if plan.apply_button:
        assert "Apply" in plan.apply_button
    if plan.submit_button:
        assert "Submit" in plan.submit_button or "submit" in plan.submit_button


def test_apply_label_with_extra_words_still_detected():
    html = """
    <form>
      <input id="email" name="email" type="email">
      <button type="button">Apply for this job</button>
      <button type="submit">Submit application</button>
    </form>
    """
    plan = build_form_plan("https://careers.example.com/role/1", html)
    assert plan.apply_button is not None
    assert "Apply" in plan.apply_button
    assert "Submit" not in plan.apply_button


def test_application_word_alone_is_not_an_apply_button():
    html = """
    <form action="/posted">
      <input id="email" name="email" type="email">
      <button type="submit">Submit application</button>
    </form>
    """
    plan = build_form_plan("https://careers.example.com/role/1", html)
    assert plan.apply_button is None
    assert plan.submit_button is not None


def test_build_form_plan_captcha_routes_unknown():
    html = (FIXTURES / "captcha_interstitial.html").read_text(encoding="utf-8")
    plan = build_form_plan("https://careers.example.com/apply", html)
    assert plan.reason == "captcha"
    assert plan.confidence == 0.0


def test_build_form_plan_linkedin_gets_floor_confidence():
    html = (FIXTURES / "easy_apply_modal.html").read_text(encoding="utf-8")
    plan = build_form_plan("https://www.linkedin.com/jobs/view/99", html)
    assert plan.ats == "linkedin_easy_apply"
    assert plan.confidence >= 0.6
    assert plan.multi_page is True


def test_is_manual_ats_lever_not_greenhouse():
    # Greenhouse stays Tier 1; Lever is listed under tier2_ats / manual_ats.
    assert is_manual_ats("https://boards.greenhouse.io/acme/jobs/1") is False
    assert is_manual_ats("https://jobs.lever.co/acme/1") is True
    assert is_manual_ats("https://careers.example.com/job/1") is False
    assert is_manual_ats("https://clever.co/jobs/1") is False


def test_build_form_plan_lever_signals_tier2():
    plan = build_form_plan("https://jobs.lever.co/acme/1", "<html></html>")
    assert plan.reason == "tier2_ats"
    assert plan.confidence == 0.0


def test_profile_value_map_splits_name():
    profile = {
        "contact": {
            "name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "123",
            "location": "Nagpur, India",
        },
        "apply_answers": {"work_authorized": "Yes", "sponsorship": "No"},
    }
    values = profile_value_map(profile)
    assert values["first_name"] == "Jane"
    assert values["last_name"] == "Doe"
    assert values["city"] == "Nagpur"
    assert values["work_authorized"] == "Yes"
