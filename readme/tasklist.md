# JobApplyPro

Personal job-search automation pipeline: parse resume once → discover jobs across LinkedIn, Indeed, Naukri, Glassdoor, Google Jobs → score → tailor resume + cover letter per job → dry-run fill applications → human-approved submit.

**How to run end-to-end:** see [`E2E_GUIDE.md`](E2E_GUIDE.md).

Full design: see `JobApplyPro_Technical_Design.md`.

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
- [x] Decide output format: ATS-friendly **DOCX + PDF** (markdown sibling for review) under `output/tailored/`
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

- [x] Write `automation/browser.py`: Playwright session setup/teardown
- [x] Write `automation/form_detector.py`: JSON-LD → CSS selector → AI extraction cascade
- [x] Write `automation/site_adapters/generic.py` (fallback adapter first — covers the most ground)
- [x] Write `automation/site_adapters/linkedin_easy_apply.py`
- [x] Write `automation/site_adapters/workday.py`
- [x] Write `core/apply.py`: pick adapter by URL/domain, fill form, screenshot, stop (dry-run default)
- [x] Update status to `apply_pending` after dry-run fill
- [x] Route unrecognized forms / CAPTCHA challenges to `manual_review` instead of failing
- [x] CLI command: `python cli.py run --stage apply --dry-run`
- [x] Manually inspect 5–10 filled-form screenshots for correctness before touching real submission
- [x] Route unrecognized forms / CAPTCHA challenges to `manual_review` instead of failing

### Phase 8 — Review & Submission Gate

- [x] Write `cli.py review`: show each `apply_pending` job with screenshot + tailored materials
- [x] Implement approve/reject per job
- [x] Implement `python cli.py run --stage apply --submit --job-id <id>` — only submits on explicit approval
- [x] Update status to `applied` or `failed` after real submission attempt
- [x] Test the full approve → submit path end-to-end on one real, low-stakes job posting

### Phase 9 — Orchestration & CLI Polish

- [x] Write `pipeline.py`: run stages in sequence, commit per job, isolate per-job failures
- [x] Write `cli.py status`: show counts per pipeline stage
- [x] Add global run-level error guard (DB connection loss, etc. exits cleanly)
- [x] Set up cron (or equivalent scheduler) for discover/score/tailor/cover_letter stages
- [x] Confirm resumability: kill a run mid-batch, restart, confirm no duplicate processing

### Phase 10 — Testing & Hardening

- [x] Unit tests for `discover.py`, `score.py`, `tailor.py`, `form_detector.py` with mocked network/LLM calls
- [x] Fixtures: sample resume, sample job postings in `tests/fixtures/` (`sample_resume.txt`, `sample_profile.json`, `jobs/batch10.json`, `forms/*.html`)
- [x] Run full pipeline against a small 10-job batch end-to-end (mocked JobSpy + LLM in `tests/test_phase10_pipeline_e2e.py`; mixed boards; 8 fit + 2 low-fit; no live employer apply)
- [x] Review logs for a full run — confirm structured logging is actually useful for debugging (`[Stage: discover]`, `[Transition: …]`, per-stage complete lines)
- [x] Document any board-specific quirks you hit (LinkedIn rate limits, Naukri field gaps, etc.) back into this README

### Phase 11 — Nice-to-haves (post-MVP, only after everything above works)

- ~~Greenhouse / Lever site adapters~~ — superseded by Tier 2 (`tasklist2.md` / ADR-002)
- ~~ATS-parseable resume PDF/DOCX~~ — done in Phase 5; still not a **designed/styled** PDF
- [x] Resume rendered as **styled/designed** PDF (visual layout) — `*.styled.pdf` sibling
- [x] Simple web dashboard instead of CLI-only status — `python cli.py dashboard`
- [x] Proxy rotation for higher-volume discovery — `PROXY_URLS` / `PROXY_FILE`
- [x] Email/Slack notification on new high-score jobs

---

## Status snapshot (keep current)

**Done:** Phases **0–11**. Canonical detail: `readme/tasklist2.md`.

**Next:** none on the checklist (Patchright / CAPTCHA / autonomous submit remain out of scope).

**Deferred / never for MVP:** Patchright, CAPTCHA solving, autonomous submit, multi-user.

---

## Known Limitations & Gaps (do not lose)

*(Phases 0–11 are on the checklist. Canonical copy also lives in `readme/tasklist2.md` — keep both in sync.)*

### Deferred / not built yet
- [ ] **Patchright stealth** — still on stock Playwright + light stealth JS
- [x] **Phase 9** — pipeline / status / DB guard / cron (**user crontab installed**); apply/submit stay manual
- [x] **Phase 10** — mocked 10-job pipeline harden + unit tests + board-quirk docs (no live-employer apply in CI)
- [x] **Phase 11** — styled PDF, dashboard, proxies, Slack/email alerts
- [ ] **CAPTCHA solving** — detect-only; no CapSolver / paid solver
- [ ] **Fully autonomous submit** — human `review --approve` required before `--submit`
- [ ] **Multi-user / auth / SaaS** — single-operator CLI only

### Apply / browser limitations
- CAPTCHA / Cloudflare / bot walls → `manual_review` (never solved)
- SSO / Google / Microsoft / Okta walls → `manual_review` (never invent credentials)
- Unsafe ID/selfie/biometric or payment/SSN prompts → hard-stop → `manual_review`
- Blocked hosts (Glassdoor, some Google careers patterns, contractor marketplaces, …) → `skipped`
- LinkedIn Easy Apply needs logged-in profile: `BROWSER_USER_DATA_DIR`
- Workday often needs logged-in profile / real Chrome (`BROWSER_CHANNEL=chrome`)
- Tier 2 headed by default (`TIER2_HEADLESS=false`); costs LLM tokens (`TIER2_MAX_JOBS_PER_RUN` / `TIER2_MAX_AGENT_STEPS`)
- Lever/Greenhouse location autocomplete improved but still imperfect on sticky widgets
- Submit loads dry-run **storage_state** then re-fills form fields
- Re-dry-run **keeps** approval — re-check the new screenshot before `--submit`
- Phase 8 e2e used a **local HTML** posting, not a live employer Submit

### Discovery / scoring / materials
- JobSpy **public search only** — not logged-in LinkedIn/Naukri scraping
- Board rate limits / sparse fields possible (Naukri fields vary)
- Scoring is LLM+embedding judgment — false positives/negatives expected
- Tailored resumes: `.docx` (upload) + `.pdf` (ATS text) + `.md` (review) + `.styled.pdf` (designed layout, not uploaded)
- Jobs already `tailored` with only `.md` need re-tailor for DOCX/PDF
- Missing tailored resume blocks auto-apply → `manual_review`

### Review / submit / DB ops
- No batch `--submit` — one approved `--job-id` at a time
- Reject → job status `skipped` (no dedicated `rejected` status)
- `python cli.py status` + `python cli.py pipeline` (optional `--include-apply` dry-run only) + `python cli.py dashboard`
- Cron: every 6h via `scripts/cron_pipeline.sh` on this machine — never `--submit` in cron
- DBs auto-upgrade via `ensure_schema()` in the CLI/pipeline DB guard
- Postgres must be reachable if `DATABASE_URL` points at it
- Always open the screenshot before approving

### ATS routing (current)
- Tier 1: LinkedIn Easy Apply, Workday, Greenhouse, high-confidence generic
- Tier 2: Lever / Workable / Ashby / TCS (`tier2_ats`) + unrecognized / low-confidence forms
- Hostname match is suffix-based (`clever.co` ≠ `lever.co`)

### Board-specific quirks (Phase 10)
- **LinkedIn discovery:** JobSpy cards omit the body unless `linkedin_fetch_description=True`. Public scrape is rate-limited / often sparse. Easy Apply needs `BROWSER_USER_DATA_DIR`.
- **Naukri:** `skills` / `experience_range` (and rating / WFH when present) appended as `--- Naukri Specific Info ---`; fields are frequently blank.
- **Indeed:** Usually the most complete public JobSpy source; still rate-limited.
- **Greenhouse:** Stays Tier 1 (`boards.greenhouse.io` + `gh_jid=`). Location autocomplete can stick.
- **Lever:** Tier 2; location autocomplete is flaky; use headed Tier 2 (`TIER2_HEADLESS=false`).
- **Workday:** Login wall is common without real Chrome + logged-in profile (`BROWSER_CHANNEL=chrome`).
- **Glassdoor / some Google careers / marketplaces:** blocked → `skipped`.
- **CI:** live Postgres tests skip when `DATABASE_URL` is down; Phase 10 e2e uses SQLite.

### Explicitly out of product scope (v1)
- Automated follow-up emails / interview scheduling
- Deep adapters for every ATS (Tier 2 covers the long tail where configured)
- Solving CAPTCHAs or creating fake accounts

---

## Notes While Building

*(Use this space to jot down anything you learn mid-build — board quirks, prompt tweaks that worked, form edge cases — so it doesn't get lost.)*

- Phase 8: Human review required before Submit. Local HTML e2e only (not a live employer).
- Phase 9: `pipeline` / `status` / DB guard / cron installed (`0 */6 * * *`). Apply/submit stay manual.
- Post–Phase 9: `TIER2_HEADLESS`, storage_state, keep approval on re-dry-run, `ensure_schema()`, ATS DOCX+PDF+md (`reportlab`).
- Phase 7 vs ApplyPilot: behavioral checklist reimplemented without CapSolver / Claude agent loop.
- Phase 0–9 audit (2026-09-11): MVP complete. Fixed Greenhouse/generic MD→DOCX upload; atomic PDF writes; sibling resume fallback.
- Phase 11 (2026-09-21): styled `*.styled.pdf`, `cli.py dashboard`, JobSpy proxies, Slack/email high-score alerts.
- Phase 10 (2026-09-11): Mocked 10-job discover→cover_letter harden; structured logs confirmed; board quirks documented. Remaining gaps: Patchright, CAPTCHA solve, live-employer submit e2e.
