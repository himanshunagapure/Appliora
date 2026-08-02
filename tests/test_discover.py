import pytest
from unittest.mock import patch
import pandas as pd
from core.discover import run_discovery
from db.session import get_db_session
from db.models import Job, RunLog
from sqlalchemy import select
import datetime

# Fixed mock URLs so we can clean them up precisely
MOCK_LINKEDIN_URL = "https://example.com/job/mock-linkedin-1"
MOCK_NAUKRI_URL   = "https://example.com/job/mock-naukri-2"

def _cleanup_mock_jobs(session):
    """Delete the two mock jobs if they exist (idempotent pre/post cleanup)."""
    for url in [MOCK_LINKEDIN_URL, MOCK_NAUKRI_URL]:
        job = session.execute(select(Job).where(Job.url == url)).scalar_one_or_none()
        if job:
            session.delete(job)

def _cleanup_mock_run_logs(session, run_log_ids: list):
    """Delete only the RunLog rows created during this test run."""
    for rid in run_log_ids:
        log = session.get(RunLog, rid)
        if log:
            session.delete(log)


def test_run_discovery_logic():
    """
    Test discovery mapping, deduplication, and database insertion logic
    using mock JobSpy responses (no network connections).
    Idempotent: pre-cleans leftover rows from prior runs.
    """
    # PRE-CLEANUP: delete any leftovers from a previous failed run
    with get_db_session() as session:
        _cleanup_mock_jobs(session)

    # Note the RunLog IDs already in DB before the test so we can isolate ours
    with get_db_session() as session:
        existing_log_ids = set(
            row[0] for row in session.execute(select(RunLog.id).where(RunLog.stage == "discover")).all()
        )

    # Create mock DataFrame representing JobSpy results
    mock_data = {
        "title":            ["Backend Developer",          "Data Analyst"],
        "company":          ["Tech Corp",                  "Data Ltd"],
        "site":             ["linkedin",                   "naukri"],
        "job_url":          [MOCK_LINKEDIN_URL,            MOCK_NAUKRI_URL],
        "description":      ["Python role description",    "Data role description"],
        "skills":           [None,                         "Python, SQL"],
        "experience_range": [None,                         "3-5 years"],
    }
    mock_df = pd.DataFrame(mock_data)

    # Patch scrape_jobs at the point it's imported inside discover.py
    with patch("core.discover.scrape_jobs") as mock_scrape:
        mock_scrape.return_value = mock_df
        run_discovery()

    # ── Assertions ──────────────────────────────────────────────────────────────
    with get_db_session() as session:

        # 1. Job 1 (LinkedIn) — no Naukri metadata
        job1 = session.execute(select(Job).where(Job.url == MOCK_LINKEDIN_URL)).scalar_one_or_none()
        assert job1 is not None,           "LinkedIn job was NOT inserted"
        assert job1.title   == "Backend Developer"
        assert job1.company == "Tech Corp"
        assert job1.source  == "linkedin"
        assert job1.status  == "discovered"
        assert "Naukri Specific Info" not in (job1.description or "")

        # 2. Job 2 (Naukri) — must have injected metadata block
        job2 = session.execute(select(Job).where(Job.url == MOCK_NAUKRI_URL)).scalar_one_or_none()
        assert job2 is not None,                              "Naukri job was NOT inserted"
        assert job2.title   == "Data Analyst"
        assert job2.company == "Data Ltd"
        assert job2.source  == "naukri"
        assert job2.status  == "discovered"
        assert "Naukri Specific Info"    in job2.description, "Naukri metadata block missing"
        assert "Skills: Python, SQL"     in job2.description, "Skills field missing"
        assert "Experience Range: 3-5 years" in job2.description, "Experience range missing"

        # 3. RunLog — find logs created by this test run
        all_log_ids = set(
            row[0] for row in session.execute(select(RunLog.id).where(RunLog.stage == "discover")).all()
        )
        new_log_ids = all_log_ids - existing_log_ids
        assert len(new_log_ids) > 0, "No RunLog entry was created by discover stage"

        # Verify jobs_processed is at least 2 in one of our new logs
        new_logs = [session.get(RunLog, rid) for rid in new_log_ids]
        assert any(log.jobs_processed >= 2 for log in new_logs), \
            f"Expected jobs_processed >= 2 in RunLog, got: {[l.jobs_processed for l in new_logs]}"

    # 4. Deduplication test — run again, should insert 0 new jobs
    with patch("core.discover.scrape_jobs") as mock_scrape:
        mock_scrape.return_value = mock_df
        run_discovery()

    with get_db_session() as session:
        # Still exactly one row per mock URL
        job1_count = session.execute(
            select(Job).where(Job.url == MOCK_LINKEDIN_URL)
        ).all()
        job2_count = session.execute(
            select(Job).where(Job.url == MOCK_NAUKRI_URL)
        ).all()
        assert len(job1_count) == 1, "Duplicate LinkedIn job was inserted!"
        assert len(job2_count) == 1, "Duplicate Naukri job was inserted!"

    # POST-CLEANUP: remove mock data so the DB stays clean
    with get_db_session() as session:
        _cleanup_mock_jobs(session)
        # Cleanup all RunLogs created by this test only
        all_log_ids_after = set(
            row[0] for row in session.execute(select(RunLog.id).where(RunLog.stage == "discover")).all()
        )
        test_created_ids = all_log_ids_after - existing_log_ids
        _cleanup_mock_run_logs(session, list(test_created_ids))
