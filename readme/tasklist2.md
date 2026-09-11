# JobApplyPro

Personal job-search automation pipeline: parse resume once → discover jobs across LinkedIn, Indeed, Naukri, Glassdoor, Google Jobs → score → tailor resume + cover letter per job → dry-run fill applications → human-approved submit.

Full design: see `JobApplyPro_Technical_Design.md` and `docs/ADR-002-generalized-apply-agent.md`.

---

## Development Task List

Check items off as you go. Tasks are grouped into phases that roughly match the order you'll build and test them in — finish a phase before moving to the next, since later phases depend on earlier ones actually working.

### Phase 0 — Project Setup

- [x] Create repo, initialize git, add `.gitignore` (include `logs/`, `.env`, `config/profile.json`)
- [x] Create folder structure from technical design (`core/`, `llm/`, `automation/`, `db/`, `utils/`, `tests/`)
- [x] Set up virtual environment, add `requirements.txt` (start with: `python-jobspy`, `sqlalchemy`, `alembic`, `pydantic-settings`, `tenacity`, `playwright`, `pytest`)
- [x] Add `.env.example` with placeholder keys (`GEMINI_API_KEY`, `DATABASE_URL`, `PINECONE_API_KEY`)
- [x] Write `config/settings.py` to load `.env` + `searches.yaml` via pydantic, fail fast on missing values
- [x] Set up `utils/logger.py` (console + rotating file handler)
- [x] Clone reference repos into `~/reference-repos/` (ApplyPilot, AloysJehwin/job-app) — read-only, not imported

### Phase 1 — Database Layer

- [x] Design `db/models.py`: `Job` table (url, title, company, source, description, status, score, reasoning, timestamps), `Application` table (job_id, tailored_resume_path, cover_letter_path, screenshot_path, submitted_at), `RunLog` table (stage, started_at, finished_at, jobs_processed, errors)
- [x] Set up Alembic migrations, run first migration
- [x] Write `db/session.py` (connection/session management)
- [x] Write a smoke test that inserts and reads back a dummy `Job` row

### Phase 2 — Resume Parsing (FR1)

- [x] Write `core/resume_parser.py`: accept PDF/DOCX, extract text
- [x] Define `profile.schema.json` (contact info, skills, experience, education, `resume_facts`)
- [x] Prompt LLM to extract structured profile from resume text
- [x] Validate extracted profile against schema
- [x] Save to `config/profile.json`
- [x] CLI command: `python cli.py init --resume <path>`
- [x] Manually verify extracted profile against your real resume for accuracy

### Phase 3 — Job Discovery (FR2)

- [x] Write `config/searches.yaml` with your actual target roles/locations
- [x] Write `core/discover.py`: wrap `jobspy.scrape_jobs()`, loop over configured searches
- [x] Write `utils/dedup.py`: dedupe by URL against existing `Job` rows
- [x] Insert new jobs into DB with `status=discovered`
- [x] Add retry/backoff wrapper (`utils/retry.py`) around JobSpy calls for 429s
- [x] CLI command: `python cli.py run --stage discover`
- [x] Smoke test: run against 1 board, `results_wanted=3`, assert non-empty + correct schema
- [x] Verify Naukri results come back correctly (skills, experience_range fields populated)

### Phase 4 — Scoring (FR3)

- [x] Set up embedding client (ChromaDB local ONNX MiniLM) in `llm/client.py`
- [x] Embed `profile.json` once, cache the vector
- [x] Write `llm/prompts/score_prompt.py`
- [x] Write `core/score.py`: cosine similarity + LLM judgment pass, combine into 1–10 score with reasoning
- [x] Update job status to `scored`, store score + reasoning on the row
- [x] CLI command: `python cli.py run --stage score`
- [x] Sanity test: unit tests with good-fit vs bad-fit sample jobs (scores separate correctly)
- [x] Set and document score threshold in config (`SCORE_THRESHOLD=6.0`, 1–10 scale)

### Phase 5 — Resume Tailoring (FR4)

- [x] Write `llm/prompts/tailor_prompt.py` with explicit "never alter resume_facts" constraint
- [x] Write `core/tailor.py`: generate tailored resume text per job
- [x] Write validation check: diff output against `resume_facts`, retry once if altered, flag `manual_review` if still failing
- [x] Decide output format (plain text / markdown / rendered PDF) and implement — **markdown** under `output/tailored/`
- [x] Update status to `tailored`
- [x] CLI command: `python cli.py run --stage tailor`
- [x] Test: unit tests cover fact preservation, retry → success, and manual_review path

### Phase 6 — Cover Letter Generation (FR5)

- [x] Write `llm/prompts/cover_letter_prompt.py`
- [x] Write `core/cover_letter.py`
- [x] Update status to `cover_letter_ready`
- [x] CLI command: `python cli.py run --stage cover_letter`
- [x] Manually review 5 generated letters for tone/accuracy

### Phase 7 — Application Automation (FR6)

**Tier 1 — known-platform adapters (done, keep as-is)**

- [x] Write `automation/browser.py`: Playwright session setup/teardown
- [x] Write `automation/form_detector.py`: JSON-LD → CSS selector → AI extraction cascade
- [x] Write `automation/site_adapters/generic.py` (fallback adapter first — covers the most ground)
- [x] Write `automation/site_adapters/linkedin_easy_apply.py`
- [x] Write `automation/site_adapters/workday.py`
- [x] Write `core/apply.py`: pick adapter by URL/domain, fill form, screenshot, stop (dry-run default)
- [x] Update status to `apply_pending` after dry-run fill
- [x] CLI command: `python cli.py run --stage apply --dry-run`
- [x] Manually inspect 5–10 filled-form screenshots for correctness before touching real submission

**Tier 2 — generalized apply agent (NEW — see ADR-002)**

- [x] Add `browser-use` dependency (MIT license) to `requirements.txt`
- [x] Write `automation/agent_apply.py`: wraps a `browser-use` `Agent` with a task prompt built from the tailored profile + resume path, instructed to stop before final submit and screenshot
- [x] Update `config/ats.yaml` routing: `tier2_ats` domains (Lever, Workable, Ashby, …) and unrecognized/low-confidence forms go to Tier 2 instead of straight to `manual_review` (Greenhouse keeps its Tier 1 adapter)
- [x] Wire Tier 2 through the *same* `automation/hard_stops.py`, dry-run submit guard, screenshot audit, and tailored-resume gate that Tier 1 already uses — no separate safety path
- [x] Route to `manual_review` only when Tier 2 itself reports low confidence, hits a CAPTCHA, or errors out
- [x] Add a per-run LLM/agent cost guard in `config/settings.py` (Tier 2 costs tokens per apply, unlike Tier 1's deterministic adapters)
- [x] Unit tests with a mocked `browser-use` agent (don't hit real sites/LLM in CI)
- [x] Manually test Tier 2 against 3–5 real Greenhouse/Lever postings currently stuck in `manual_review`, review screenshots

**Tier 3 — manual review (existing, unchanged)**

- [x] Route unrecognized forms / CAPTCHA challenges to `manual_review` instead of failing

**Stealth layer (separate, deferred — not required to close Phase 7)**

- [ ] Evaluate swapping `automation/browser.py`'s Playwright launch for Patchright (drop-in, reduces automation-signal detection) — roll out to Tier 1 and Tier 2 together once validated in isolation first

### Phase 8 — Review & Submission Gate

- [x] Write `cli.py review`: show each `apply_pending` job with screenshot + tailored materials
- [x] Implement approve/reject per job
- [x] Implement `python cli.py run --stage apply --submit --job-id <id>` — only submits on explicit approval
- [x] Update status to `applied` or `failed` after real submission attempt
- [x] Test the full approve → submit path end-to-end on one real, low-stakes job posting

### Phase 9 — Orchestration & CLI Polish

- [ ] Write `pipeline.py`: run stages in sequence, commit per job, isolate per-job failures
- [ ] Write `cli.py status`: show counts per pipeline stage
- [ ] Add global run-level error guard (DB connection loss, etc. exits cleanly)
- [ ] Set up cron (or equivalent scheduler) for discover/score/tailor/cover_letter stages
- [ ] Confirm resumability: kill a run mid-batch, restart, confirm no duplicate processing

### Phase 10 — Testing & Hardening

- [ ] Unit tests for `discover.py`, `score.py`, `tailor.py`, `form_detector.py` with mocked network/LLM calls
- [ ] Fixtures: sample resume, sample job postings in `tests/fixtures/`
- [ ] Run full pipeline against a real but small batch (e.g. 10 jobs) end-to-end
- [ ] Review logs for a full run — confirm structured logging is actually useful for debugging
- [ ] Document any board-specific quirks you hit (LinkedIn rate limits, Naukri field gaps, etc.) back into this README

### Phase 11 — Nice-to-haves (post-MVP, only after everything above works)

- ~~Greenhouse / Lever site adapters~~ — superseded by Tier 2 generalized agent, no longer needed
- [ ] Resume rendered as styled PDF instead of plain text
- [ ] Simple web dashboard instead of CLI-only status
- [ ] Proxy rotation for higher-volume discovery
- [ ] Email/Slack notification on new high-score jobs

---

## Notes While Building

*(Use this space to jot down anything you learn mid-build — board quirks, prompt tweaks that worked, form edge cases — so it doesn't get lost.)*

- Phase 7: CAPTCHA is detected only (never solved). LinkedIn Easy Apply needs a logged-in persistent Chromium profile via `BROWSER_USER_DATA_DIR`. Domains under `tier2_ats` / `manual_ats` in `config/ats.yaml` (Lever, Workable, Ashby, …) route to the Tier 2 `browser-use` agent instead of immediate `manual_review`. Greenhouse keeps its Tier 1 deterministic adapter. Unrecognized / low-confidence generic forms also fall through to Tier 2. Dry-run never clicks final Submit; real submit requires `--submit --job-id` on an **approved** `apply_pending` job.
- Phase 7 vs ApplyPilot: ApplyPilot drives apply via Claude+Playwright MCP (AGPL) + CapSolver. We reimplemented the *behavioral* checklist deterministically — hard stops (`automation/hard_stops.py`), blocked sites/SSO, dry-run submit guard, screenshot audit, tailored-resume gate — without CapSolver or the Claude agent loop.
- Phase 7 revision (see ADR-002): rather than keep hand-writing a new adapter per ATS, we're adding `browser-use` (MIT) as a Tier 2 generalized fallback agent for anything `config/ats.yaml` doesn't already route to a known adapter. Tier 1 adapters (LinkedIn, Workday, Greenhouse, high-confidence generic) stay as the fast/cheap/deterministic path since they're already built and tested — Tier 2 only picks up what would otherwise hit `manual_review` (configured `tier2_ats` domains + unrecognized forms). Per-run cost guard: `TIER2_MAX_JOBS_PER_RUN` / `TIER2_MAX_AGENT_STEPS`. All existing safety infra wraps both tiers unchanged. CAPTCHA handling is explicitly still out of scope — Tier 2 doesn't solve CAPTCHAs any better than Tier 1 does.
- Phase 7 screenshot review (2026-09-10): Inspected 10+ dry-run shots.
  - **Pass / correct dry-run:** LinkedIn Easy Apply (`job_152_*`, `debug_easy_apply_after_review`) — contact + tailored DOCX resume on 3/3 review, Submit not clicked. Tier 2 Lever Spotify + Kitware and Greenhouse Figma + Cloudflare — forms progressed to end with Submit visible but not clicked (`live_tier2_batch_*.png`); batch result `filled=4`.
  - **Expected stops:** Workday (`job_155_*`) — account/sign-in wall (needs logged-in profile). Headless Tier 2 often hits CAPTCHA — use `BROWSER_HEADLESS=false`.
  - **Quirks fixed in agent prompt:** Open Apply/Application tab before filling; scroll to top before done for audit; fill disability Name/Date when present; Lever location autocomplete still flaky.
  - **Phase 7 MVP closed.** Patchright stealth remains optional/deferred. Next product gate is Phase 8 (review → approve → submit).
- Phase 7 review pass (2026-09-10): Fixed `outcome=filled` being mapped to `submitted` in submit mode; tightened Tier 2 “core fields filled” heuristic so profile dumps are not treated as a successful fill; hostname matching is suffix-based (`clever.co` is not `lever.co`); `detect_ats` now recognizes Workable, company-hosted Lever iframes, and `*.myworkdaysite.com` (not only `wd1`). Tier 2 now closes the browser-use Browser after the agent run.
- Phase 8: `python cli.py review` shows apply_pending jobs (screenshot + resume/cover excerpts). Approve with `--approve --job-id` (or `--interactive`); reject → `skipped`. `python cli.py run --stage apply --submit --job-id` refuses unless `applications.review_decision=approved`. Successful submit → `applied` + `submitted_at`; failure → `failed`. Low-stakes e2e is a local HTML form (Playwright), not a live employer posting.
