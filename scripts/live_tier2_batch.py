"""
Batch live dry-run for Tier 2 across several public ATS apply URLs.

NEVER submits. Uses real profile + tailored resume.
Headed browser (less CAPTCHA than headless).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from automation.agent_apply import run_tier2_apply  # noqa: E402
from config.settings import get_settings  # noqa: E402

PROFILE = ROOT / "config" / "profile.json"
RESUME = ROOT / "output" / "tailored" / "job_117_harmonic_software-engineer.md"
OUT_DIR = ROOT / "output" / "screenshots"
SUMMARY = ROOT / "output" / "live_tier2_batch_results.json"

JOBS = [
    {
        "id": "spotify_lever",
        "label": "Spotify — Android Engineer (Lever)",
        "url": "https://jobs.lever.co/spotify/2193db3f-77c5-43b8-b030-8f92c9882bf1/apply",
    },
    {
        "id": "kitware_ai_lever",
        "label": "Kitware — AI Researcher (Lever)",
        "url": "https://jobs.lever.co/kitware/102aa9fd-44bd-4c54-b49c-b15935e058e7/apply",
    },
    {
        "id": "figma_greenhouse",
        "label": "Figma — AE Bengaluru (Greenhouse, Tier 2 agent)",
        "url": "https://boards.greenhouse.io/figma/jobs/5579204004?gh_jid=5579204004",
    },
    {
        "id": "cloudflare_greenhouse",
        "label": "Cloudflare — AI Security Research (Greenhouse, Tier 2)",
        "url": "https://boards.greenhouse.io/cloudflare/jobs/8097321?gh_jid=8097321",
    },
]

ATS_CFG = {
    "tier2_ats": ["lever.co", "jobs.lever.co", "ashbyhq.com", "workable.com"],
    "manual_ats": ["lever.co", "jobs.lever.co"],
    "blocked_sso": [
        "accounts.google.com",
        "login.microsoftonline.com",
        "okta.com",
    ],
    "captcha_signals": ["g-recaptcha", "h-captcha", "cf-turnstile"],
    "blocked": {"sites": [], "url_patterns": []},
}


def main() -> int:
    settings = get_settings()
    settings.tier2_max_agent_steps = max(settings.tier2_max_agent_steps, 45)
    # Headed reduces bot-wall CAPTCHAs vs the prior headless batch.
    settings.browser_headless = False

    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    if not RESUME.exists():
        print(f"Missing resume: {RESUME}", file=sys.stderr)
        return 2

    resume_text = RESUME.read_text(encoding="utf-8")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    print(f"Batch Tier 2 dry-run: {len(JOBS)} jobs, max_steps={settings.tier2_max_agent_steps}")
    print(f"Model: {settings.tier2_llm_model or settings.gemini_model}")
    print(f"Headless: {settings.browser_headless}\n")

    for i, job in enumerate(JOBS, 1):
        shot = OUT_DIR / f"live_tier2_batch_{job['id']}.png"
        print("=" * 72)
        print(f"[{i}/{len(JOBS)}] {job['label']}")
        print(f"URL: {job['url']}")
        t0 = time.time()
        materials = {
            "resume_path": str(RESUME),
            "cover_letter_path": None,
            "cover_letter_text": "",
            "tailored_resume_text": resume_text,
            "job_url": job["url"],
        }
        try:
            result = run_tier2_apply(
                job_url=job["url"],
                profile=profile,
                materials=materials,
                dry_run=True,
                screenshot_path=shot,
                ats_config=ATS_CFG,
            )
            elapsed = round(time.time() - t0, 1)
            row = {
                "id": job["id"],
                "label": job["label"],
                "url": job["url"],
                "status": result.status,
                "adapter": result.adapter,
                "error": result.error,
                "screenshot": result.screenshot_path,
                "screenshot_exists": bool(
                    result.screenshot_path and Path(result.screenshot_path).exists()
                ),
                "elapsed_sec": elapsed,
            }
        except Exception as exc:
            elapsed = round(time.time() - t0, 1)
            row = {
                "id": job["id"],
                "label": job["label"],
                "url": job["url"],
                "status": "failed",
                "adapter": "browser_use_tier2",
                "error": f"exception: {exc}",
                "screenshot": None,
                "screenshot_exists": False,
                "elapsed_sec": elapsed,
            }
        results.append(row)
        print(
            f"→ status={row['status']}  elapsed={row['elapsed_sec']}s  "
            f"shot={row['screenshot_exists']}  error={row['error']!r}"
        )
        print()

    SUMMARY.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("=" * 72)
    print("SUMMARY")
    filled = sum(1 for r in results if r["status"] == "filled")
    review = sum(1 for r in results if r["status"] == "manual_review")
    failed = sum(1 for r in results if r["status"] not in {"filled", "manual_review"})
    print(f"filled={filled}  manual_review={review}  other={failed}")
    print(f"Wrote {SUMMARY}")
    for r in results:
        print(f"  - {r['id']}: {r['status']} ({r['elapsed_sec']}s)")

    ok = all(r["status"] in {"filled", "manual_review"} for r in results)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
