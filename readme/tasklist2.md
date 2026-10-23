# JobApplyPro

Personal job-search automation pipeline: parse resume once → discover jobs across LinkedIn, Indeed, Naukri, Glassdoor, Google Jobs → score → tailor resume + cover letter per job → dry-run fill applications → human-approved submit.

**How to run end-to-end:** see [`E2E_GUIDE.md`](E2E_GUIDE.md).

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
- [x] Decide output format (plain text / markdown / rendered PDF) and implement — **ATS-friendly DOCX + PDF** (markdown sibling for review) under `output/tailored/`
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

- [x] Write `pipeline.py`: run stages in sequence, commit per job, isolate per-job failures
- [x] Write `cli.py status`: show counts per pipeline stage
- [x] Add global run-level error guard (DB connection loss, etc. exits cleanly)
- [x] Set up cron (or equivalent scheduler) for discover/score/tailor/cover_letter stages
- [x] Confirm resumability: kill a run mid-batch, restart, confirm no duplicate processing

### Phase 10 — Testing & Hardening

- [x] Unit tests for `discover.py`, `score.py`, `tailor.py`, `form_detector.py` with mocked network/LLM calls
- [x] Fixtures: sample resume, sample job postings in `tests/fixtures/` (`sample_resume.txt`, `sample_profile.json`, `jobs/batch10.json`, `forms/*.html`)
- [x] Run full pipeline against a small 10-job batch end-to-end (mocked JobSpy + LLM in `tests/test_phase10_pipeline_e2e.py`; mixed LinkedIn/Greenhouse/Lever/Indeed/Naukri/Workday; 8 fit + 2 low-fit; no live employer apply)
- [x] Review logs for a full run — confirm structured logging is actually useful for debugging (`[Stage: discover]`, `[Transition: …]`, per-stage complete lines; failed scores isolated as `Failed to score job`)
- [x] Document any board-specific quirks you hit (LinkedIn rate limits, Naukri field gaps, etc.) back into this README

### Phase 11 — Nice-to-haves (post-MVP, only after everything above works)

- ~~Greenhouse / Lever site adapters~~ — superseded by Tier 2 generalized agent, no longer needed
- ~~ATS-parseable resume PDF/DOCX~~ — done in Phase 5 (`output/tailored/*.docx` + `*.pdf` + `*.md`); still not a **designed/styled** PDF
- [x] Resume rendered as **styled/designed** PDF (visual layout) — `output/tailored/job_*.styled.pdf` sibling; ATS `.pdf` unchanged
- [x] Simple web dashboard instead of CLI-only status — `python cli.py dashboard` (127.0.0.1:8765, read-only)
- [x] Proxy rotation for higher-volume discovery — `PROXY_URLS` / `PROXY_FILE` passed to JobSpy (credentials redacted in logs)
- [x] Email/Slack notification on new high-score jobs — `NOTIFY_SLACK_WEBHOOK_URL` and/or SMTP (`NOTIFY_EMAIL_TO`); no-op if unset

---

## Status snapshot (keep current)

**Done:** Phases **0–11** — MVP (0–10) plus styled PDF, local dashboard, JobSpy proxy rotation, and Slack/email high-score alerts.

**Next:** none on the checklist. Remaining is deferred/out-of-scope (Patchright, CAPTCHA solve, autonomous submit, multi-user).

**Deferred / never for MVP:** Patchright stealth, CAPTCHA solving, autonomous submit, multi-user SaaS.

---

## Known Limitations & Gaps (do not lose)

*(Phases 0–11 are on the checklist. MVP is 0–10. Phase 11 nice-to-haves are implemented. Everything below is intentional scope, known fragility, deferred work, or operational gotchas — keep this list updated when you hit new ones.)*

### Deferred / not built yet
- [ ] **Patchright stealth** — still on stock Playwright + light `_install_stealth` JS; evaluate Patchright later for Tier 1 + Tier 2 together (reduces bot-signal detection)
- [x] **Phase 9** — `pipeline.py`, `cli.py status`, DB run guard, cron helper + **user crontab installed** (`0 */6 * * *` → `scripts/cron_pipeline.sh`); apply/submit stay manual
- [x] **Phase 10** — mocked 10-job pipeline harden + unit tests + board-quirk docs (no live-employer apply in CI)
- [x] **Phase 11** — styled `*.styled.pdf`, `cli.py dashboard`, JobSpy `PROXY_URLS`/`PROXY_FILE`, Slack/email high-score alerts (no-op until configured)
- [ ] **CAPTCHA solving** — detect-only forever for MVP; no CapSolver / paid solver (ToS + ban risk)
- [ ] **Fully autonomous submit** — human `review --approve` is required before `--submit`
- [ ] **Multi-user / auth / SaaS** — single-operator CLI tool only

### Apply / browser limitations
- CAPTCHA / Cloudflare / bot walls → `manual_review` (never solved)
- SSO / Google / Microsoft / Okta login walls → `manual_review` (never invent credentials)
- Unsafe ID/selfie/biometric or payment/SSN prompts → hard-stop → `manual_review`
- Blocked hosts (Glassdoor, some Google careers patterns, marketplaces like Upwork/Toptal, …) → `skipped`
- LinkedIn Easy Apply needs a logged-in persistent Chromium profile: `BROWSER_USER_DATA_DIR`
- Workday often needs logged-in profile / real Chrome (`BROWSER_CHANNEL=chrome`); account walls are expected without it
- Tier 2 (`browser-use`) uses **headed by default** (`TIER2_HEADLESS=false`, independent of `BROWSER_HEADLESS`) — headless hits CAPTCHA much more; set `TIER2_HEADLESS=true` only intentionally
- Tier 2 costs LLM tokens per attempt; capped by `TIER2_MAX_JOBS_PER_RUN` / `TIER2_MAX_AGENT_STEPS`
- Tier 2 outcome parsing is heuristic (narrative + `outcome=…`); sticky autocomplete can still confuse the agent judge
- Lever / Greenhouse location autocomplete: short city + ArrowDown/Enter/Tab fallback after 2 tries (still imperfect on sticky widgets)
- Submit loads prior dry-run **storage_state** then re-fills form fields (browser process cannot stay open between dry-run and approve)
- Re-dry-run on an approved job **keeps** `review_decision=approved` — re-open the new screenshot before `--submit`
- Phase 8 submit e2e used a **local HTML posting**, not a live employer Submit
- No Patchright / advanced anti-detect yet (see deferred)

### Discovery / scoring / materials limitations
- Discovery uses JobSpy **public search only** — not logged-in LinkedIn/Naukri scraping
- Rate limits / empty boards / JobSpy quirks can yield sparse or partial fields (Naukri fields vary). Optional `PROXY_URLS` / `PROXY_FILE` rotate per scrape; JobSpy still uses public search only.
- Scoring is LLM+embedding judgment — false positives/negatives expected
- Tailored resumes write **four siblings** under `output/tailored/`: `.docx` (primary ATS upload), `.pdf` (ATS text PDF), `.md` (review), `.styled.pdf` (designed layout — never uploaded). Layout quality still depends on the LLM following the tailor prompt.
- Jobs already `tailored` with only `.md` stay until re-tailored (status reset or new tailor run)
- Cover letters / tailored text need human spot-checks; `resume_facts` are protected but tone/fit is not perfect
- Missing tailored resume blocks auto-apply → `manual_review` (`missing_materials`)

### Review / submit / DB operational gaps
- No batch `--submit` — one approved `--job-id` at a time
- Reject sets job status `skipped` (not a dedicated `rejected` status)
- `python cli.py status` + `python cli.py pipeline` (discover→score→tailor→cover_letter; optional `--include-apply` dry-run only) + `python cli.py dashboard` (local read-only UI)
- Cron installed for this machine: every 6h via `scripts/cron_pipeline.sh` → `logs/cron_pipeline.log` — never put `--submit` in cron
- Schema: CLI/pipeline DB guard runs `ensure_schema()` (`alembic upgrade head`) automatically
- Postgres must be up if `DATABASE_URL` points at it (pipeline aborts cleanly on connection loss)
- Screenshots may show EEO footer / overview if the agent scrolled wrong — always open the file before approving

### ATS routing quirks (current behavior)
- Tier 1: LinkedIn Easy Apply, Workday, Greenhouse, high-confidence generic
- Tier 2: `tier2_ats` / legacy `manual_ats` (Lever, Workable, Ashby, TCS, …) + unrecognized / low-confidence forms + company-hosted Lever/Ashby/Workable fingerprints
- Greenhouse stays Tier 1 deterministic adapter
- Hostname matching is suffix-based (`clever.co` ≠ `lever.co`)

### Board-specific quirks (Phase 10)
- **LinkedIn discovery:** JobSpy search cards omit the job body unless `linkedin_fetch_description=True` (always set in `run_discovery`). Public scrape is rate-limited / often sparse; empty descriptions used to stick as `"nan"` until backfill. Easy Apply still needs a logged-in persistent profile (`BROWSER_USER_DATA_DIR`).
- **Naukri:** `skills`, `experience_range`, `company_rating`, and `work_from_home_type` are appended under `--- Naukri Specific Info ---` when present. Those fields are frequently blank on public JobSpy results — do not assume they are populated.
- **Indeed:** Usually the most complete public JobSpy source here; still subject to board rate limits and duplicate URLs.
- **Greenhouse:** Stays on the Tier 1 deterministic adapter (`boards.greenhouse.io` and `gh_jid=` company pages). Location autocomplete can still stick; short city + ArrowDown/Enter/Tab after 2 tries.
- **Lever:** `jobs.lever.co` (and company-hosted Lever fingerprints) route to **Tier 2**. Location autocomplete is the flakiest widget; headed Tier 2 (`TIER2_HEADLESS=false`) avoids a lot of CAPTCHA.
- **Workday:** `*.myworkdayjobs.com` / `*.myworkdaysite.com` are Tier 1 but commonly hit an account/sign-in wall without a real Chrome profile (`BROWSER_CHANNEL=chrome` + `BROWSER_USER_DATA_DIR`).
- **Glassdoor / some Google careers / contractor marketplaces:** blocked hosts → `skipped` (never apply).
- **Apply routing:** suffix host match only (`clever.co` is not `lever.co`). Submit reloads dry-run `storage_state` then re-fills fields — the browser process cannot stay open between dry-run and `--submit`.
- **CI / live DB:** `test_db` and live-Postgres `test_run_discovery_logic` skip when `DATABASE_URL` is unreachable. Phase 10 e2e uses in-memory SQLite.

### Explicitly out of product scope (v1)
- Automated follow-up emails / interview scheduling
- Deep per-ATS adapters for every platform (superseded by Tier 2 agent where listed)
- Anything that requires solving CAPTCHAs or creating fake accounts

---

## Notes While Building

*(Use this space to jot down anything you learn mid-build — board quirks, prompt tweaks that worked, form edge cases — so it doesn't get lost.)*

- Phase 7: CAPTCHA is detected only (never solved). LinkedIn Easy Apply needs a logged-in persistent Chromium profile via `BROWSER_USER_DATA_DIR`. Domains under `tier2_ats` / `manual_ats` in `config/ats.yaml` (Lever, Workable, Ashby, …) route to the Tier 2 `browser-use` agent instead of immediate `manual_review`. Greenhouse keeps its Tier 1 deterministic adapter. Unrecognized / low-confidence generic forms also fall through to Tier 2. Dry-run never clicks final Submit; real submit requires `--submit --job-id` on an **approved** `apply_pending` job.
- Phase 7 vs ApplyPilot: ApplyPilot drives apply via Claude+Playwright MCP (AGPL) + CapSolver. We reimplemented the *behavioral* checklist deterministically — hard stops (`automation/hard_stops.py`), blocked sites/SSO, dry-run submit guard, screenshot audit, tailored-resume gate — without CapSolver or the Claude agent loop.
- Phase 7 revision (see ADR-002): rather than keep hand-writing a new adapter per ATS, we're adding `browser-use` (MIT) as a Tier 2 generalized fallback agent for anything `config/ats.yaml` doesn't already route to a known adapter. Tier 1 adapters (LinkedIn, Workday, Greenhouse, high-confidence generic) stay as the fast/cheap/deterministic path since they're already built and tested — Tier 2 only picks up what would otherwise hit `manual_review` (configured `tier2_ats` domains + unrecognized forms). Per-run cost guard: `TIER2_MAX_JOBS_PER_RUN` / `TIER2_MAX_AGENT_STEPS`. All existing safety infra wraps both tiers unchanged. CAPTCHA handling is explicitly still out of scope — Tier 2 doesn't solve CAPTCHAs any better than Tier 1 does.
- Phase 7 screenshot review (2026-09-10): Inspected 10+ dry-run shots.
  - **Pass / correct dry-run:** LinkedIn Easy Apply (`job_152_*`, `debug_easy_apply_after_review`) — contact + tailored DOCX resume on 3/3 review, Submit not clicked. Tier 2 Lever Spotify + Kitware and Greenhouse Figma + Cloudflare — forms progressed to end with Submit visible but not clicked (`live_tier2_batch_*.png`); batch result `filled=4`.
  - **Expected stops:** Workday (`job_155_*`) — account/sign-in wall (needs logged-in profile). Headless Tier 2 often hits CAPTCHA — use headed Tier 2 (`TIER2_HEADLESS=false`).
  - **Quirks fixed in agent prompt:** Open Apply/Application tab before filling; scroll to top before done for audit; fill disability Name/Date when present; Lever location autocomplete still flaky.
  - **Phase 7 MVP closed.** Patchright stealth remains optional/deferred. Next product gate was Phase 8 (review → approve → submit).
- Phase 7 review pass (2026-09-10): Fixed `outcome=filled` being mapped to `submitted` in submit mode; tightened Tier 2 “core fields filled” heuristic; hostname matching is suffix-based; `detect_ats` recognizes Workable, company-hosted Lever iframes, and `*.myworkdaysite.com`. Tier 2 closes the browser-use Browser after the agent run.
- Phase 8: `python cli.py review` shows apply_pending jobs. Approve with `--approve --job-id` (or `--interactive`); reject → `skipped`. `--submit` requires `review_decision=approved`. Low-stakes e2e is a local HTML form, not a live employer.
- Phase 9: `pipeline` / `status` / DB guard / cron. User crontab installed (`0 */6 * * *`). Apply/submit stay manual. Resumability via status filters + URL dedupe (unit-tested).
- Post–Phase 9 ops fixes: Tier 2 headed via `TIER2_HEADLESS`; storage_state across dry-run→submit; approval kept on re-dry-run; `ensure_schema()` auto-migrates; ATS resumes emit DOCX+PDF+markdown (`reportlab`).
- Phase 0–9 audit (2026-09-11): Feature-complete for MVP. Fixed Greenhouse/generic to call `ensure_uploadable_resume` (legacy `.md` no longer uploaded raw); PDF writes are atomic (temp→rename); apply materials fall back to sibling `.docx`/`.pdf`/`.md` if stored path is missing.
- Phase 10 (2026-09-11): Mocked 10-job discover→cover_letter (`tests/fixtures/jobs/batch10.json` + `tests/test_phase10_pipeline_e2e.py`). Structured logs confirmed useful (`[Stage:]` / `[Transition:]` / per-stage complete / isolated `Failed to score job`). Live Postgres tests skip when DB is down. Board quirks documented above. Remaining intentional gaps: Patchright, CAPTCHA solve, live-employer submit e2e.
- Phase 11 (2026-09-21): Styled `*.styled.pdf` (ATS `.pdf` unchanged); `python cli.py dashboard`; JobSpy proxy rotation via `PROXY_URLS`/`PROXY_FILE`; Slack/email alerts on newly scored ≥ threshold jobs (no-op until configured).
