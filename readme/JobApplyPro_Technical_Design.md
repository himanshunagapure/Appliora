# JobApplyPro — Technical Design Document

Version 1.0 | Plain modular Python architecture

---

## 1. What We're Building

JobApplyPro is a personal job-search automation pipeline. You give it your resume once. It then, on a schedule:

1. Parses your resume into structured, reusable data.
2. Searches LinkedIn, Indeed, Naukri, Glassdoor, and Google Jobs for open roles.
3. Scores each job against your profile so you only spend attention on good matches.
4. Tailors your resume and writes a cover letter per job, without inventing experience.
5. Pre-fills the application form (dry-run by default; you approve before anything is submitted).
6. Tracks every job's status end-to-end in a database you can query.

It is a single-user, self-hosted CLI tool — not a multi-tenant SaaS product (see Scope, §10).

---

## 2. Repos We're Using

| Repo | License | How we use it |
|---|---|---|
| `speedyapply/JobSpy` | MIT | **Installed as a pip dependency**, used directly in code. This is the only repo whose code we actually run. |
| `Pickle-Pixel/ApplyPilot` | AGPL-3.0 | **Reference only.** We clone it locally to study its stage design, scoring rubric, and 3-tier form-detection cascade (JSON-LD → CSS selectors → AI extraction). We do **not** copy or import its code, because AGPL would force us to open-source anything we build on top of it. We reimplement the same *ideas* independently. |
| `AloysJehwin/job-app` | MIT | **Reference only.** We don't use n8n. We borrow the conceptual pattern (per-applicant tracking record, resume version links, status log) and reimplement it as a Postgres table instead of Google Sheets. |
| Local-first ATS resume optimizer (`ats-resume-checker` pattern) | — | **Concept only.** We borrow the "analyze → generate → re-score" loop idea for the tailoring stage's self-check step. |

### 2.1 How to get them locally

```bash
# Actual runtime dependency — this is the only one that ends up in requirements.txt
pip install python-jobspy

# Reference-only clones — read for design ideas, never imported into our code
mkdir -p ~/reference-repos && cd ~/reference-repos
git clone https://github.com/Pickle-Pixel/ApplyPilot.git
git clone https://github.com/AloysJehwin/job-app.git
```

Keep the reference clones outside the `jobapplypro/` project folder entirely, so there's no risk of accidentally vendoring AGPL code into a repo you might later want to keep proprietary.

### 2.2 Exactly what we take from each

- **JobSpy**: the `scrape_jobs()` function itself, called directly. It already returns Naukri-specific fields (`skills`, `experience_range`, `company_rating`, `work_from_home_type`), so no separate Naukri scraper is needed.
- **ApplyPilot**: the *shape* of the pipeline (Discover → Enrich → Score → Tailor → Cover Letter → Apply), the rule "tailoring must preserve resume_facts verbatim, never fabricate," and the 3-tier fallback pattern for reading unfamiliar application forms. All reimplemented in our own modules.
- **AloysJehwin/job-app**: the idea of one tracking record per job with a status field and a link to the generated resume version — reimplemented as a SQL table, not a Google Sheet.

---

## 3. Requirements

### 3.1 Functional Requirements

**FR1 — Resume ingestion**
- Accept a resume as PDF or DOCX, once.
- Extract structured `profile.json`: contact info, work authorization, skills, experience entries, education, and `resume_facts` (companies, titles, dates, metrics) that must never be altered by later AI steps.

**FR2 — Job discovery**
- Run scheduled searches across LinkedIn, Indeed, Naukri, Glassdoor, Google Jobs via JobSpy.
- Search parameters (roles, locations, remote preference, recency window) are config-driven, not hardcoded.
- Deduplicate jobs by URL across runs — a job seen yesterday isn't re-processed today.

**FR3 — Scoring**
- Every new job gets a 1–10 fit score against the profile, combining semantic similarity (embeddings) and an LLM judgment pass.
- Only jobs above a configurable threshold proceed to tailoring.

**FR4 — Resume tailoring**
- Generate a per-job resume variant: reorder/emphasize relevant experience, inject missing ATS keywords from the job description.
- Hard constraint: company names, titles, dates, and quantified achievements in `resume_facts` must appear unchanged. The tailoring step is checked against this before being accepted.

**FR5 — Cover letter generation**
- One short, job-specific cover letter per tailored application, referencing the company and role.

**FR6 — Application form handling**
- Detect and fill known form types (LinkedIn Easy Apply, Workday, generic HTML forms) using Playwright.
- **Dry-run is the default mode**: forms are filled but never submitted without explicit confirmation.
- Unrecognized or CAPTCHA-blocked forms are marked `manual_review`, not silently failed or force-submitted.

**FR7 — Tracking**
- Every job has one row with a status: `discovered → scored → tailored → cover_letter_ready → apply_pending → applied / failed / manual_review / skipped`.
- CLI command to view current status counts and drill into any job's history.

**FR8 — Configuration-driven operation**
- Search parameters, scoring threshold, and which stages to run are all set via config files, not code edits.

### 3.2 Non-Functional Requirements

- **Resumability**: if the process is killed mid-run, already-completed jobs aren't reprocessed (state is committed per-job, not per-batch).
- **Cost control**: default LLM calls use a free tier (Gemini free tier: 15 RPM / 1M tokens per day) with usage logged so you can see when you're close to a limit.
- **Rate-limit respect**: discovery stage backs off and retries on 429s rather than hammering job boards (per JobSpy's own guidance — Indeed tolerates volume, LinkedIn does not).
- **Auditability**: every AI-generated artifact (tailored resume, cover letter, score + reasoning) is saved to disk/DB, not just used transiently — so you can review why a job scored the way it did.

---

## 4. Architecture Diagram

```
                          ┌────────────────────┐
                          │   profile.json      │  (one-time, from resume_parser.py)
                          └─────────┬──────────┘
                                    │
      ┌─────────────────────────────┼─────────────────────────────┐
      │                            │                              │
      ▼                            ▼                              ▼
┌───────────┐            ┌──────────────────┐           ┌──────────────────┐
│ discover.py│──JobSpy──▶│  jobs table       │           │ Pinecone/local    │
│ (cron)     │           │  (status=         │◀─embed────│ embedding of      │
└───────────┘            │  discovered)      │           │ profile           │
                          └────────┬──────────┘           └──────────────────┘
                                    │
                                    ▼
                          ┌──────────────────┐
                          │   score.py        │  cosine sim + LLM judge
                          │   (status=scored)  │
                          └────────┬──────────┘
                              score ≥ threshold
                                    │
                                    ▼
                          ┌──────────────────┐
                          │   tailor.py        │  rewrite + self-check against
                          │   (status=tailored) │  resume_facts (never fabricate)
                          └────────┬──────────┘
                                    │
                                    ▼
                          ┌──────────────────┐
                          │ cover_letter.py    │
                          └────────┬──────────┘
                                    │
                                    ▼
                          ┌──────────────────┐
                          │   apply.py          │  Playwright, dry-run default,
                          │  (status=applied/    │  human approval gate
                          │  manual_review)       │
                          └──────────────────┘
                                    │
                                    ▼
                          ┌──────────────────┐
                          │  cli.py status     │  reporting / review
                          └──────────────────┘
```

---

## 5. Project Structure

```
jobapplypro/
├── README.md
├── requirements.txt
├── .env.example
├── config/
│   ├── searches.yaml          # roles, locations, boards, recency window, results wanted
│   ├── profile.json           # your parsed resume — generated once by resume_parser.py
│   └── settings.py            # loads & validates .env + yaml via pydantic
│
├── core/
│   ├── __init__.py
│   ├── resume_parser.py       # PDF/DOCX -> structured profile.json (FR1)
│   ├── discover.py            # wraps JobSpy, dedupes, writes to jobs table (FR2)
│   ├── score.py                # embeddings + LLM scoring (FR3)
│   ├── tailor.py                # resume rewriting + resume_facts validation (FR4)
│   ├── cover_letter.py          # cover letter generation (FR5)
│   └── apply.py                  # orchestrates automation/ for form fill/submit (FR6)
│
├── llm/
│   ├── __init__.py
│   ├── client.py                # thin wrapper around Gemini/OpenAI/Ollama, swappable
│   └── prompts/
│       ├── score_prompt.py
│       ├── tailor_prompt.py
│       └── cover_letter_prompt.py
│
├── automation/
│   ├── __init__.py
│   ├── browser.py                # Playwright session management
│   ├── form_detector.py          # JSON-LD -> CSS selectors -> AI extraction cascade
│   └── site_adapters/
│       ├── linkedin_easy_apply.py
│       ├── workday.py
│       └── generic.py            # fallback for unknown ATS
│
├── db/
│   ├── __init__.py
│   ├── models.py                  # SQLAlchemy: Job, Application, RunLog
│   ├── session.py
│   └── migrations/                # Alembic
│
├── utils/
│   ├── logger.py                  # structured logging setup (§8)
│   ├── retry.py                   # tenacity-based retry/backoff decorators
│   └── dedup.py                    # URL-based job deduplication
│
├── pipeline.py                     # runs core/* stages in sequence, per-job commit
├── cli.py                          # `python cli.py run --stage discover`, `status`, `review`
│
├── tests/
│   ├── test_discover.py
│   ├── test_score.py
│   ├── test_tailor.py
│   ├── test_form_detector.py
│   └── fixtures/                   # sample job postings, sample resume
│
└── logs/                            # rotating run logs, gitignored
```

### 5.1 What lives in each key file

- **`resume_parser.py`**: takes a PDF/DOCX path, uses an LLM to extract structured fields, validates against a JSON schema, writes `config/profile.json`. Run once, re-run only if the resume changes.
- **`discover.py`**: reads `config/searches.yaml`, calls `jobspy.scrape_jobs()` per search config, dedupes against existing `jobs` table rows by URL, inserts new rows with `status=discovered`.
- **`score.py`**: for each `discovered` job, embeds the job description, computes cosine similarity against the profile embedding, sends both to the LLM for a 1–10 judgment with reasoning, writes score + reasoning to the row, updates `status=scored`.
- **`tailor.py`**: for jobs above threshold, prompts the LLM to rewrite resume content, then runs a validation pass that diffs the output against `resume_facts` — if any protected fact was altered, it retries once, then flags for manual review if it still fails.
- **`apply.py`**: picks the right `site_adapters/*` based on the job URL's domain/ATS signature, calls `form_detector.py` to map job fields to form fields, fills the form via `browser.py`, screenshots the filled form, and stops — submission only happens if you explicitly confirm via `cli.py review`.
- **`pipeline.py`**: the only orchestration logic — a plain loop that runs each stage over jobs in the right status, commits per job (not per batch), and logs failures without halting the whole run.

---

## 6. Data Flow

1. **Once**: You run `python cli.py init --resume resume.pdf`. `resume_parser.py` produces `config/profile.json`.
2. **Scheduled (e.g. every 6 hours via cron)**: `python cli.py run --stage discover` calls JobSpy across all configured boards, inserts new `jobs` rows.
3. **Scheduled**: `python cli.py run --stage score` processes all `discovered` jobs, scores them, updates status.
4. **Scheduled**: `python cli.py run --stage tailor` processes all `scored` jobs above threshold, generates tailored resume + cover letter, updates status to `cover_letter_ready`.
5. **Manual trigger**: `python cli.py run --stage apply --dry-run` fills forms for all `cover_letter_ready` jobs, saves screenshots, sets status `apply_pending`.
6. **Manual**: `python cli.py review` shows you each `apply_pending` job with its filled screenshot and tailored materials. You approve or reject per job.
7. **Manual, only on approval**: `python cli.py run --stage apply --submit --job-id <id>` actually clicks submit for that one job.

Every stage reads only jobs in the status it expects and writes the next status — this is what makes the pipeline resumable and safe to re-run.

---

## 7. How We'll Know It's Working

- **Unit tests** per module with mocked LLM and browser calls (no real API/network calls in CI).
- **Smoke test**: `discover.py` against one board, `results_wanted=5`, asserts non-empty results and correct schema.
- **Scoring sanity check**: run against 10 hand-picked jobs (5 obviously good fits, 5 obviously bad fits) and confirm the scores separate correctly.
- **Tailoring validation test**: assert that `resume_facts` fields are byte-identical between input profile and output resume for a batch of sample jobs.
- **Dry-run apply review**: for a sample of 5–10 jobs, manually inspect the filled-form screenshots for correctness before ever enabling real submission.
- **`cli.py status`**: gives live counts per pipeline stage — the ongoing operational health check (e.g. "40 discovered, 12 scored above threshold, 3 stuck in manual_review").

---

## 8. Error Handling and Logging

- **Logging**: Python `logging` module, structured (timestamp, stage, job_id, level, message), written to both console and a rotating file in `logs/`. One log line per state transition.
- **Retries**: `utils/retry.py` wraps all network/LLM calls with exponential backoff (via `tenacity`), capped at 3 attempts. JobSpy 429s trigger a longer cooldown per board, not an immediate retry.
- **Per-job isolation**: an exception on one job (bad HTML, malformed job description, LLM timeout) is caught, logged with the job's ID, and the job is marked `failed` with the error message stored on the row — it never stops the rest of the batch.
- **Unknown forms**: `form_detector.py` returning low confidence, or a CAPTCHA challenge appearing, routes the job to `manual_review` rather than guessing or crashing.
- **Global run guard**: `pipeline.py` wraps each stage call in a try/except at the run level too, so a systemic failure (e.g. DB connection lost) is logged clearly and the run exits cleanly instead of leaving partial, uncommitted state.

---

## 9. Configuration

- **`.env`** (secrets, gitignored): `GEMINI_API_KEY`, `DATABASE_URL`, `PINECONE_API_KEY` (optional if using local embeddings instead).
- **`config/searches.yaml`**: list of search configs — roles, locations, boards, `hours_old`, `results_wanted`, remote preference. Adding a new search is a YAML edit, not a code change.
- **`config/profile.json`**: generated once, editable by hand if you want to correct extraction errors.
- **`config/settings.py`**: loads both of the above via `pydantic-settings`, validates types/required fields at startup, fails fast with a clear error if something's missing — rather than failing deep inside a pipeline run.

---

## 10. Scope

### In scope (MVP)
- Resume parsing (PDF/DOCX → structured profile).
- Job discovery across LinkedIn, Indeed, Naukri, Glassdoor, Google Jobs via JobSpy.
- Embedding + LLM-based scoring.
- Per-job resume tailoring with fact-preservation guarantee.
- Per-job cover letter generation.
- Dry-run application form filling for LinkedIn Easy Apply, Workday, and generic HTML forms.
- Human-approved submission (no fully autonomous submit in v1).
- SQL-based job/application tracking with CLI status reporting.

### Explicitly out of scope (for now)
- Fully autonomous submission without human review.
- CAPTCHA solving (would require a paid solver service).
- Proxy rotation / infrastructure for high-volume scraping.
- Web or mobile UI — CLI only in v1.
- Multi-user support, auth, or SaaS deployment.
- Deep integrations for Greenhouse, Lever, and other ATS platforms beyond Workday/generic (candidates for v2).
- Automated follow-up emails or interview scheduling.
- Anything that requires logging into LinkedIn/Naukri directly (JobSpy's public search only — logged-in scraping carries much higher ban risk).
