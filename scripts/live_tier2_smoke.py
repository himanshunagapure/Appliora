"""
Live dry-run smoke for Tier 2 (ADR-002).

Uses real profile + tailored resume against a public Lever apply URL.
NEVER submits — dry_run=True and the agent prompt forbids final Submit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from automation.agent_apply import run_tier2_apply  # noqa: E402
from config.settings import get_settings  # noqa: E402


JOB_URL = "https://jobs.lever.co/kitware/0dade915-7f07-437e-8ddf-c346f70acd1d/apply"
RESUME = ROOT / "output" / "tailored" / "job_117_harmonic_software-engineer.md"
PROFILE = ROOT / "config" / "profile.json"
SHOT = ROOT / "output" / "screenshots" / "live_tier2_kitware_smoke.png"


def main() -> int:
    settings = get_settings()
    # Use full configured budget — previous 25-step cap caused incomplete fills.
    settings.tier2_max_agent_steps = max(settings.tier2_max_agent_steps, 50)
    if settings.browser_headless is False:
        print("BROWSER_HEADLESS=false — agent will open a visible Chrome window")

    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    if not RESUME.exists():
        print(f"Missing resume: {RESUME}", file=sys.stderr)
        return 2

    materials = {
        "resume_path": str(RESUME),
        "cover_letter_path": None,
        "cover_letter_text": "",
        "tailored_resume_text": RESUME.read_text(encoding="utf-8"),
        "job_url": JOB_URL,
    }

    print(f"Starting Tier 2 dry-run against:\n  {JOB_URL}")
    print(f"Resume: {RESUME}")
    print(f"Max steps: {settings.tier2_max_agent_steps}")
    print(f"Model: {settings.tier2_llm_model or settings.gemini_model}")

    result = run_tier2_apply(
        job_url=JOB_URL,
        profile=profile,
        materials=materials,
        dry_run=True,
        screenshot_path=SHOT,
        ats_config={
            "tier2_ats": ["lever.co", "jobs.lever.co"],
            "manual_ats": ["lever.co", "jobs.lever.co"],
            "blocked_sso": [
                "accounts.google.com",
                "login.microsoftonline.com",
                "okta.com",
            ],
            "captcha_signals": ["g-recaptcha", "h-captcha", "cf-turnstile"],
            "blocked": {"sites": [], "url_patterns": []},
        },
    )

    print("\n=== RESULT ===")
    print(f"status:     {result.status}")
    print(f"adapter:    {result.adapter}")
    print(f"error:      {result.error}")
    print(f"screenshot: {result.screenshot_path}")
    if result.screenshot_path and Path(result.screenshot_path).exists():
        print(f"screenshot_bytes: {Path(result.screenshot_path).stat().st_size}")
    else:
        print("screenshot_bytes: (missing)")

    # Success for this smoke: dry-run filled (maps to apply_pending in pipeline).
    ok = result.status == "filled" and result.adapter == "browser_use_tier2"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
