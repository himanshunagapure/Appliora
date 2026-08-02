# JobApplyPro

Personal job-search automation pipeline: parse resume once → discover jobs across LinkedIn, Indeed, Naukri, Glassdoor, Google Jobs → score → tailor resume + cover letter per job → dry-run fill applications → human-approved submit.

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

- [ ] Set up embedding client (ChromaDB or local) in `llm/client.py`
- [ ] Embed `profile.json` once, cache the vector
- [ ] Write `llm/prompts/score_prompt.py`
- [ ] Write `core/score.py`: cosine similarity + LLM judgment pass, combine into 1–10 score with reasoning
- [ ] Update job status to `scored`, store score + reasoning on the row
- [ ] CLI command: `python cli.py run --stage score`
- [ ] Sanity test: run against 5 obviously-good-fit + 5 obviously-bad-fit sample jobs, confirm scores separate correctly
- [ ] Set and document your score threshold in config

### Phase 5 — Resume Tailoring (FR4)

- [ ] Write `llm/prompts/tailor_prompt.py` with explicit "never alter resume_facts" constraint
- [ ] Write `core/tailor.py`: generate tailored resume text per job
- [ ] Write validation check: diff output against `resume_facts`, retry once if altered, flag `manual_review` if still failing
- [ ] Decide output format (plain text / markdown / rendered PDF) and implement
- [ ] Update status to `tailored`
- [ ] CLI command: `python cli.py run --stage tailor`
- [ ] Test: run against 5 sample jobs, manually confirm no fabricated experience or altered facts

### Phase 6 — Cover Letter Generation (FR5)

- [ ] Write `llm/prompts/cover_letter_prompt.py`
- [ ] Write `core/cover_letter.py`
- [ ] Update status to `cover_letter_ready`
- [ ] CLI command: `python cli.py run --stage cover_letter`
- [ ] Manually review 5 generated letters for tone/accuracy

### Phase 7 — Application Automation (FR6)

- [ ] Write `automation/browser.py`: Playwright session setup/teardown
- [ ] Write `automation/form_detector.py`: JSON-LD → CSS selector → AI extraction cascade
- [ ] Write `automation/site_adapters/generic.py` (fallback adapter first — covers the most ground)
- [ ] Write `automation/site_adapters/linkedin_easy_apply.py`
- [ ] Write `automation/site_adapters/workday.py`
- [ ] Write `core/apply.py`: pick adapter by URL/domain, fill form, screenshot, stop (dry-run default)
- [ ] Update status to `apply_pending` after dry-run fill
- [ ] Route unrecognized forms / CAPTCHA challenges to `manual_review` instead of failing
- [ ] CLI command: `python cli.py run --stage apply --dry-run`
- [ ] Manually inspect 5–10 filled-form screenshots for correctness before touching real submission

### Phase 8 — Review & Submission Gate

- [ ] Write `cli.py review`: show each `apply_pending` job with screenshot + tailored materials
- [ ] Implement approve/reject per job
- [ ] Implement `python cli.py run --stage apply --submit --job-id <id>` — only submits on explicit approval
- [ ] Update status to `applied` or `failed` after real submission attempt
- [ ] Test the full approve → submit path end-to-end on one real, low-stakes job posting

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

- [ ] Greenhouse / Lever site adapters
- [ ] Resume rendered as styled PDF instead of plain text
- [ ] Simple web dashboard instead of CLI-only status
- [ ] Proxy rotation for higher-volume discovery
- [ ] Email/Slack notification on new high-score jobs

---

## Notes While Building

*(Use this space to jot down anything you learn mid-build — board quirks, prompt tweaks that worked, form edge cases — so it doesn't get lost.)*

-
