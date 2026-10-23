# JobApplyPro — End-to-End Run Guide

How to go from a fresh clone to discovered → scored → tailored → dry-run filled → human-approved → submitted.

Apply/submit is **never** automatic. Cron and `pipeline` stop before submit; you review screenshots and approve each job yourself.

---

## What the pipeline does

```
init (once)
  → discover → score → tailor → cover_letter
  → apply --dry-run
  → review (approve / reject)
  → apply --submit --job-id <id>
```

| Stage | Command | What it produces |
|---|---|---|
| Init | `python cli.py init --resume …` | `config/profile.json` |
| Discover | `--stage discover` | New `Job` rows (`status=discovered`) |
| Score | `--stage score` | Scores + reasoning; optional Slack/email alerts |
| Tailor | `--stage tailor` | `output/tailored/job_* .{md,docx,pdf,styled.pdf}` |
| Cover letter | `--stage cover_letter` | `output/cover_letters/…` |
| Apply dry-run | `--stage apply --dry-run` | Filled form + screenshot; `status=apply_pending` |
| Review | `python cli.py review …` | Approve or reject |
| Submit | `--stage apply --submit --job-id N` | Real submit (approved jobs only) |

Orchestrator (cron-safe, no submit):

```bash
python cli.py pipeline                  # discover → score → tailor → cover_letter
python cli.py pipeline --include-apply  # same + apply dry-run
```

---

## 1. One-time setup

```bash
cd /path/to/Project10
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Playwright browser (or use system Chrome — see below)
playwright install chromium
```

Copy env and edit:

```bash
cp .env.example .env
```

**Required in `.env`:**

```env
GEMINI_API_KEY=your_key_here
DATABASE_URL=sqlite:///./jobapplypro.db
SCORE_THRESHOLD=6.0
```

SQLite is fine for personal use. Postgres works if you set `DATABASE_URL` accordingly — the CLI auto-runs `alembic upgrade head` before stages.

**Edit searches** in `config/searches.yaml` (roles, locations, boards, `results_wanted`). Start small (`results_wanted: 3`) for a first live run.

**Optional browser login** (LinkedIn Easy Apply / Workday):

```env
BROWSER_USER_DATA_DIR=/home/you/.jobapplypro-browser
BROWSER_CHANNEL=chrome          # use installed Chrome if Playwright Chromium is missing
BROWSER_HEADLESS=false          # easier for first login
TIER2_HEADLESS=false            # Tier 2 agent — less CAPTCHA when headed
```

---

## 2. Parse your resume (once)

```bash
python cli.py init --resume /path/to/your_resume.pdf
```

Opens/creates `config/profile.json`. **Read it before continuing** — later stages treat `resume_facts` as ground truth and refuse to invent degrees, employers, etc.

Re-run `init` only when the resume changes.

---

## 3. Full E2E (recommended first run)

### A. Automated materials pipeline

```bash
python cli.py pipeline
```

Equivalent stage-by-stage:

```bash
python cli.py run --stage discover
python cli.py run --stage score
python cli.py run --stage tailor
python cli.py run --stage cover_letter
```

Check progress anytime:

```bash
python cli.py status
python cli.py dashboard    # http://127.0.0.1:8765
```

Inspect artifacts:

- Tailored resumes → `output/tailored/` (upload **`.docx`** or ATS **`.pdf`**; `*.styled.pdf` is visual-only)
- Cover letters → `output/cover_letters/`
- Logs → `logs/`

### B. Dry-run apply (fills forms, never submits)

```bash
python cli.py run --stage apply --dry-run
# or one job:
python cli.py run --stage apply --dry-run --job-id 42
```

Or fold dry-run into the orchestrator:

```bash
python cli.py pipeline --include-apply
```

Screenshots land in `output/screenshots/`. Jobs move to `apply_pending`.

### C. Human review (required gate)

List pending:

```bash
python cli.py review
python cli.py review --open          # also open screenshots
python cli.py review --interactive   # walk each job approve/reject/skip
```

Approve or reject one job:

```bash
python cli.py review --approve --job-id 42
python cli.py review --reject --job-id 42 --reason "wrong location"
```

**Always open the screenshot** before approving — confirm contact fields, resume upload, and that Submit was not clicked in dry-run.

### D. Real submit (one approved job at a time)

```bash
python cli.py run --stage apply --submit --job-id 42
```

`--submit` refuses jobs that are not `apply_pending` **and** `review_decision=approved`. There is no batch submit on purpose.

---

## 4. Day-to-day / cron

Safe scheduled path (never submits):

```bash
# every 6 hours — see scripts/crontab.example
chmod +x scripts/cron_pipeline.sh
# crontab -e → add:
# 0 */6 * * * /path/to/Project10/scripts/cron_pipeline.sh >> /path/to/Project10/logs/cron_pipeline.log 2>&1
```

Morning check:

```bash
python cli.py status
python cli.py review --interactive
```

Then submit only what you approved.

---

## 5. Optional Phase 11 knobs

| Feature | How |
|---|---|
| Styled resume PDF | Automatic sibling `*.styled.pdf` under `output/tailored/` (not for ATS upload) |
| Dashboard | `python cli.py dashboard` → `http://127.0.0.1:8765` |
| Proxy rotation (discovery) | `PROXY_URLS=…` and/or `PROXY_FILE=config/proxies.txt` (see `config/proxies.example.txt`) |
| Slack / email on high scores | Set `NOTIFY_SLACK_WEBHOOK_URL` and/or `NOTIFY_EMAIL_TO` + SMTP fields in `.env` |

Unconfigured notify/proxy settings are no-ops — they do not break the pipeline.

---

## 6. Job status cheat sheet

```
discovered → scored → tailored → cover_letter_ready
  → apply_pending → applied
                 ↘ skipped (rejected)
  failed / manual_review / skipped (blocked boards, CAPTCHA, etc.)
```

`python cli.py status` and the dashboard show counts for each.

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `Database unavailable` / `Connection refused` to localhost | Postgres is up on the **host**, but Cursor agent shells cannot TCP to host `127.0.0.1`. Run `sudo ./scripts/setup_pg_socket.sh` once. Host terminal: `pg_isready -h 127.0.0.1 -p 5432`. |
| `GEMINI_API_KEY` / LLM errors | Set a valid key in `.env`; model default is `gemini-2.5-flash` |
| Empty discovery | Boards rate-limit or block (Naukri recaptcha is common). Shrink `results_wanted`, add proxies, retry later |
| Playwright / Chromium missing | `playwright install chromium` **or** `BROWSER_CHANNEL=chrome` |
| LinkedIn Easy Apply wants login | Set `BROWSER_USER_DATA_DIR`, run headed once, sign in, then re-run dry-run |
| Workday sign-in wall | Same persistent Chrome profile + `BROWSER_CHANNEL=chrome` |
| Tier 2 CAPTCHA / stuck forms | `TIER2_HEADLESS=false`; open screenshot → `manual_review` if needed |
| `--submit` refused | Job must be approved first (`review --approve --job-id`) |
| Stale tailored `.md` only | Re-run tailor after status reset, or tailor new jobs |

More board quirks: see **Board-specific quirks** in `readme/tasklist2.md`.

---

## 8. Smoke-test without live boards / LLM

Unit + mocked e2e (no network, no Gemini):

```bash
source .venv/bin/activate
pytest tests/ -q
```

Phase 10 mocked 10-job pipeline:

```bash
pytest tests/test_phase10_pipeline_e2e.py -v
```

---

## Quick copy-paste (happy path)

```bash
source .venv/bin/activate
cp .env.example .env   # then set GEMINI_API_KEY

python cli.py init --resume ~/Documents/resume.pdf
# → review config/profile.json

# edit config/searches.yaml (small results_wanted for first run)

python cli.py pipeline
python cli.py status

python cli.py run --stage apply --dry-run
python cli.py review --interactive --open

python cli.py run --stage apply --submit --job-id <id>
```

Design notes: `readme/JobApplyPro_Technical_Design.md`. Checklist / known gaps: `readme/tasklist2.md`.
