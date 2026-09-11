"""Unit tests for Tier 2 browser-use agent wrapper — mocked agent, no real LLM/sites."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from automation.agent_apply import (
    ADAPTER_NAME,
    Tier2Budget,
    build_agent_task,
    is_tier2_ats,
    run_tier2_apply,
    should_use_tier2,
)
from automation.hard_stops import detect_hard_stop


class _FakeHistory:
    def __init__(
        self,
        *,
        final: str = "",
        successful: bool | None = True,
        done: bool = True,
        errors: list | None = None,
        urls: list[str] | None = None,
        screenshots: list[str] | None = None,
    ):
        self._final = final
        self._successful = successful
        self._done = done
        self._errors = errors or []
        self._urls = urls or []
        self._screenshots = screenshots or []

    def final_result(self):
        return self._final

    def is_successful(self):
        return self._successful

    def is_done(self):
        return self._done

    def errors(self):
        return self._errors

    def urls(self):
        return self._urls

    def screenshot_paths(self):
        return self._screenshots


def test_is_tier2_ats_lever_not_greenhouse():
    assert is_tier2_ats("https://jobs.lever.co/acme/1") is True
    assert is_tier2_ats("https://boards.greenhouse.io/acme/jobs/1") is False
    assert is_tier2_ats("https://careers.example.com/job/1") is False
    # Query-string mentions of Lever must not steal LinkedIn/Workday URLs.
    assert (
        is_tier2_ats(
            "https://www.linkedin.com/jobs/view/1?external=https://jobs.lever.co/acme/1"
        )
        is False
    )
    assert is_tier2_ats("https://clever.co/jobs/1") is False


def test_lever_is_not_hard_stop_anymore():
    # ADR-002: tier2_ats domains go to the agent, not immediate manual_review.
    assert detect_hard_stop("https://jobs.lever.co/acme/1", "") is None


def test_build_agent_task_includes_dry_run_guard_and_resume():
    profile = {
        "contact": {
            "name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "555",
            "location": "Austin, TX",
        }
    }
    materials = {
        "resume_path": "/tmp/resume.md",
        "cover_letter_path": "/tmp/cover.txt",
        "cover_letter_text": "Dear hiring manager",
        "tailored_resume_text": "Jane Doe — engineer",
    }
    task = build_agent_task(
        job_url="https://jobs.lever.co/acme/1",
        profile=profile,
        materials=materials,
        dry_run=True,
    )
    assert "NEVER click the final Submit" in task
    assert "/tmp/resume.md" in task or "resume" in task.lower()
    assert "jane@example.com" in task
    assert "outcome=filled" in task
    assert "ONLY fill fields that are actually visible" in task
    assert "At most TWO attempts per field" in task
    assert "Location / city autocomplete" in task
    assert "Application tab" in task or "Apply button" in task
    assert "scroll to the TOP" in task or "scroll to the top" in task.lower()


def test_should_use_tier2_for_unrecognized_and_budget_domains():
    assert (
        should_use_tier2(
            "https://jobs.lever.co/acme/1",
            plan_reason=None,
            plan_ats="generic",
            plan_confidence=0.9,
            adapter_name="generic",
            form_min_confidence=0.55,
        )
        is True
    )
    assert (
        should_use_tier2(
            "https://careers.example.com/apply",
            plan_reason="unrecognized_form",
            plan_ats="unknown",
            plan_confidence=0.1,
            adapter_name="generic",
            form_min_confidence=0.55,
        )
        is True
    )
    assert (
        should_use_tier2(
            "https://careers.example.com/apply",
            plan_reason=None,
            plan_ats="generic",
            plan_confidence=0.9,
            adapter_name="generic",
            form_min_confidence=0.55,
        )
        is False
    )
    assert (
        should_use_tier2(
            "https://boards.greenhouse.io/acme/jobs/1",
            plan_reason=None,
            plan_ats="greenhouse",
            plan_confidence=0.8,
            adapter_name="greenhouse",
            form_min_confidence=0.55,
        )
        is False
    )
    assert (
        should_use_tier2(
            "https://careers.acme.com/jobs/1",
            plan_reason=None,
            plan_ats="ashby",
            plan_confidence=0.8,
            adapter_name="generic",
            form_min_confidence=0.55,
        )
        is True
    )
    assert (
        should_use_tier2(
            "https://jobs.example.com/apply",
            plan_reason=None,
            plan_ats="workable",
            plan_confidence=0.7,
            adapter_name="generic",
            form_min_confidence=0.55,
        )
        is True
    )


def test_run_tier2_apply_success_with_mocked_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    # Avoid loading real .env searches if settings already cached — patch settings.
    from config import settings as settings_mod

    fake_settings = MagicMock()
    fake_settings.tier2_enabled = True
    fake_settings.tier2_max_agent_steps = 10
    fake_settings.tier2_max_jobs_per_run = 5
    fake_settings.tier2_llm_model = ""
    fake_settings.gemini_model = "gemini-2.5-flash"
    fake_settings.gemini_api_key = "test-key"
    fake_settings.browser_headless = True
    fake_settings.browser_user_data_dir = None
    monkeypatch.setattr(settings_mod, "_settings", fake_settings)
    monkeypatch.setattr("automation.agent_apply.get_settings", lambda: fake_settings)

    resume = tmp_path / "resume.md"
    resume.write_text("# Jane Doe\n")

    async def fake_runner(**kwargs):
        assert "NEVER click the final Submit" in kwargs["task"]
        # Markdown resumes are converted to DOCX before the agent runs.
        assert kwargs["resume_path"].endswith(".docx")
        assert Path(kwargs["resume_path"]).exists()
        return _FakeHistory(
            final="Form complete. outcome=filled",
            successful=True,
            done=True,
            urls=["https://jobs.lever.co/acme/1"],
            screenshots=[str(tmp_path / "shot.png")],
        )

    result = run_tier2_apply(
        job_url="https://jobs.lever.co/acme/1",
        profile={
            "contact": {
                "name": "Jane Doe",
                "email": "jane@example.com",
                "phone": "1",
                "location": "x",
            }
        },
        materials={"resume_path": str(resume)},
        dry_run=True,
        agent_runner=fake_runner,
        ats_config={"tier2_ats": ["lever.co"], "blocked_sso": [], "captcha_signals": []},
    )
    assert result.status == "filled"
    assert result.adapter == ADAPTER_NAME


def test_run_tier2_apply_captcha_routes_manual_review(tmp_path, monkeypatch):
    from config import settings as settings_mod

    fake_settings = MagicMock()
    fake_settings.tier2_enabled = True
    fake_settings.tier2_max_agent_steps = 10
    fake_settings.tier2_llm_model = ""
    fake_settings.gemini_model = "gemini-2.5-flash"
    fake_settings.gemini_api_key = "test-key"
    fake_settings.browser_headless = True
    fake_settings.browser_user_data_dir = None
    monkeypatch.setattr(settings_mod, "_settings", fake_settings)
    monkeypatch.setattr("automation.agent_apply.get_settings", lambda: fake_settings)

    resume = tmp_path / "r.md"
    resume.write_text("x")

    async def fake_runner(**kwargs):
        return _FakeHistory(final="Blocked by CAPTCHA. outcome=captcha")

    result = run_tier2_apply(
        job_url="https://jobs.lever.co/acme/1",
        profile={"contact": {"name": "A B", "email": "a@b.c", "phone": "1", "location": "x"}},
        materials={"resume_path": str(resume)},
        agent_runner=fake_runner,
        ats_config={"tier2_ats": ["lever.co"], "blocked_sso": []},
    )
    assert result.status == "manual_review"
    assert result.error == "captcha"


def test_run_tier2_budget_exhausted(tmp_path, monkeypatch):
    from config import settings as settings_mod

    fake_settings = MagicMock()
    fake_settings.tier2_enabled = True
    fake_settings.tier2_max_agent_steps = 10
    fake_settings.tier2_llm_model = ""
    fake_settings.gemini_model = "gemini-2.5-flash"
    fake_settings.gemini_api_key = "test-key"
    fake_settings.browser_headless = True
    fake_settings.browser_user_data_dir = None
    monkeypatch.setattr("automation.agent_apply.get_settings", lambda: fake_settings)

    resume = tmp_path / "r.md"
    resume.write_text("x")
    budget = Tier2Budget(max_jobs=1)
    assert budget.try_consume() is True

    called = {"n": 0}

    async def fake_runner(**kwargs):
        called["n"] += 1
        return _FakeHistory(final="outcome=filled")

    result = run_tier2_apply(
        job_url="https://jobs.lever.co/acme/1",
        profile={"contact": {"name": "A B", "email": "a@b.c", "phone": "1", "location": "x"}},
        materials={"resume_path": str(resume)},
        budget=budget,  # already consumed 1/1
        agent_runner=fake_runner,
        ats_config={"tier2_ats": ["lever.co"], "blocked_sso": []},
    )
    assert result.status == "manual_review"
    assert "tier2_budget_exhausted" in (result.error or "")
    assert called["n"] == 0


def test_run_tier2_disabled(tmp_path, monkeypatch):
    fake_settings = MagicMock()
    fake_settings.tier2_enabled = False
    monkeypatch.setattr("automation.agent_apply.get_settings", lambda: fake_settings)

    result = run_tier2_apply(
        job_url="https://jobs.lever.co/acme/1",
        profile={},
        materials={"resume_path": str(tmp_path / "r.md")},
    )
    assert result.status == "manual_review"
    assert "tier2_disabled" in (result.error or "")


def test_low_confidence_outcome(tmp_path, monkeypatch):
    fake_settings = MagicMock()
    fake_settings.tier2_enabled = True
    fake_settings.tier2_max_agent_steps = 5
    fake_settings.tier2_llm_model = ""
    fake_settings.gemini_model = "gemini-2.5-flash"
    fake_settings.gemini_api_key = "k"
    fake_settings.browser_headless = True
    fake_settings.browser_user_data_dir = None
    monkeypatch.setattr("automation.agent_apply.get_settings", lambda: fake_settings)

    resume = tmp_path / "r.md"
    resume.write_text("x")

    async def fake_runner(**kwargs):
        return _FakeHistory(final="Too ambiguous. outcome=low_confidence")

    result = run_tier2_apply(
        job_url="https://jobs.lever.co/acme/1",
        profile={"contact": {"name": "A B", "email": "a@b.c", "phone": "1", "location": "x"}},
        materials={"resume_path": str(resume)},
        agent_runner=fake_runner,
        ats_config={"tier2_ats": ["lever.co"], "blocked_sso": []},
    )
    assert result.status == "manual_review"
    assert "low_confidence" in (result.error or "")


def _patch_tier2_settings(monkeypatch) -> MagicMock:
    fake_settings = MagicMock()
    fake_settings.tier2_enabled = True
    fake_settings.tier2_max_agent_steps = 10
    fake_settings.tier2_llm_model = ""
    fake_settings.gemini_model = "gemini-2.5-flash"
    fake_settings.gemini_api_key = "test-key"
    fake_settings.browser_headless = True
    fake_settings.browser_user_data_dir = None
    monkeypatch.setattr("automation.agent_apply.get_settings", lambda: fake_settings)
    return fake_settings


def test_run_tier2_copies_agent_screenshot(tmp_path, monkeypatch):
    _patch_tier2_settings(monkeypatch)
    resume = tmp_path / "r.md"
    resume.write_text("x")
    src = tmp_path / "agent_shot.png"
    src.write_bytes(b"png")
    dest = tmp_path / "audit" / "job_1_tier2.png"

    async def fake_runner(**kwargs):
        return _FakeHistory(
            final="outcome=filled",
            screenshots=[str(src)],
        )

    result = run_tier2_apply(
        job_url="https://jobs.lever.co/acme/1",
        profile={"contact": {"name": "A B", "email": "a@b.c", "phone": "1", "location": "x"}},
        materials={"resume_path": str(resume)},
        screenshot_path=dest,
        agent_runner=fake_runner,
        ats_config={"tier2_ats": ["lever.co"], "blocked_sso": []},
    )
    assert result.status == "filled"
    assert result.screenshot_path == str(dest)
    assert dest.is_file()
    assert dest.read_bytes() == b"png"


def test_run_tier2_dry_run_submit_is_recorded(tmp_path, monkeypatch):
    _patch_tier2_settings(monkeypatch)
    resume = tmp_path / "r.md"
    resume.write_text("x")

    async def fake_runner(**kwargs):
        return _FakeHistory(final="Clicked submit. outcome=submitted")

    result = run_tier2_apply(
        job_url="https://jobs.lever.co/acme/1",
        profile={"contact": {"name": "A B", "email": "a@b.c", "phone": "1", "location": "x"}},
        materials={"resume_path": str(resume)},
        dry_run=True,
        agent_runner=fake_runner,
        ats_config={"tier2_ats": ["lever.co"], "blocked_sso": []},
    )
    assert result.status == "submitted"
    assert "dry_run_violated" in (result.error or "")


def test_core_fill_narrative_counts_as_filled_even_if_judge_fails():
    from automation.agent_apply import _history_to_fill_result

    history = _FakeHistory(
        final=(
            "I have filled Full name (Jane Doe), Email (jane@example.com), "
            "Phone (555). I uploaded the resume file. I was unable to input "
            "Current location. The application is partially filled and stopped "
            "before submission as requested."
        ),
        successful=False,
        done=True,
    )
    result = _history_to_fill_result(history, dry_run=True)
    assert result.status == "filled"


def test_outcome_filled_is_never_submitted_in_submit_mode():
    from automation.agent_apply import _history_to_fill_result

    history = _FakeHistory(final="Form complete. outcome=filled", successful=True)
    result = _history_to_fill_result(history, dry_run=False)
    assert result.status == "filled"


def test_profile_dump_without_fill_verbs_is_not_core_filled():
    from automation.agent_apply import _core_fields_filled, _history_to_fill_result

    dump = (
        "Could not fill the form. Login required. Profile has Full name, "
        "Email (jane@example.com), Phone."
    )
    assert _core_fields_filled(dump) is False
    history = _FakeHistory(final=dump + " outcome=failed", successful=False)
    result = _history_to_fill_result(history, dry_run=True)
    assert result.status == "manual_review"


def test_missing_resume_does_not_consume_budget(tmp_path, monkeypatch):
    _patch_tier2_settings(monkeypatch)
    budget = Tier2Budget(max_jobs=1)
    result = run_tier2_apply(
        job_url="https://jobs.lever.co/acme/1",
        profile={},
        materials={"resume_path": None},
        budget=budget,
        ats_config={"tier2_ats": ["lever.co"], "blocked_sso": []},
    )
    assert result.status == "manual_review"
    assert "missing_materials" in (result.error or "")
    assert budget.used == 0
