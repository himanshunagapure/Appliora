"""Hard-stop / policy routing tests (no browser)."""

from automation.hard_stops import detect_hard_stop, is_url_blocked, hard_stop_to_fill_status


def test_is_url_blocked_glassdoor():
    assert is_url_blocked("https://www.glassdoor.com/job/123") is True
    assert is_url_blocked("https://careers.example.com/job/1") is False


def test_detect_expired_job():
    html = "<p>Sorry, this job is closed and we are no longer accepting applications.</p>"
    stop = detect_hard_stop("https://careers.example.com/job/1", html)
    assert stop is not None
    assert stop.code == "expired"
    assert hard_stop_to_fill_status(stop) == "expired"


def test_detect_already_applied():
    html = "<div>You have already applied for this job.</div>"
    stop = detect_hard_stop("https://careers.example.com/job/1", html)
    assert stop is not None
    assert stop.code == "already_applied"


def test_already_applied_ignored_on_workday_sign_in():
    html = (
        "<h1>Sign In</h1><p>Forgot your password?</p>"
        "<script>already applied</script>"
    )
    stop = detect_hard_stop(
        "https://workday.wd5.myworkdayjobs.com/en-US/Workday/job/x",
        html,
    )
    assert stop is None or stop.code != "already_applied"


def test_detect_unsafe_verification():
    html = "<p>Please take a selfie and upload a government-issued ID.</p>"
    stop = detect_hard_stop("https://careers.example.com/job/1", html)
    assert stop is not None
    assert stop.code == "unsafe_verification"
    assert stop.outcome == "manual_review"


def test_detect_payment_fields():
    html = "<label>Social Security Number <input name='ssn'></label>"
    stop = detect_hard_stop("https://careers.example.com/job/1", html)
    assert stop is not None
    assert stop.code == "unsafe_payment"


def test_detect_marketplace_host():
    stop = detect_hard_stop("https://www.upwork.com/jobs/1", "")
    assert stop is not None
    assert stop.code == "not_a_job_application"


def test_detect_sso():
    stop = detect_hard_stop("https://accounts.google.com/o/oauth2", "")
    assert stop is not None
    assert stop.code == "sso_required"


def test_safe_page_returns_none():
    html = "<form><input name='email'><button>Apply</button></form>"
    assert detect_hard_stop("https://careers.example.com/apply", html) is None
