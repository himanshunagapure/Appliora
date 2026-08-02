import pytest
from sqlalchemy import select
from db.session import get_db_session
from db.models import Job, Application, RunLog

def test_db_connection_and_crud():
    """
    Smoke test to verify we can connect, insert, read, update,
    and delete Job, Application, and RunLog records.
    """
    test_url = "https://example.com/jobs/test-python-dev-12345"
    test_stage = "test_smoke"

    with get_db_session() as session:
        # 1. Clean up any leftovers from prior aborted runs
        existing_job = session.execute(select(Job).filter_by(url=test_url)).scalar_one_or_none()
        if existing_job:
            session.delete(existing_job)
            session.commit()
            
        existing_log = session.execute(select(RunLog).filter_by(stage=test_stage)).scalar_one_or_none()
        if existing_log:
            session.delete(existing_log)
            session.commit()

    # 2. Insert a Job
    with get_db_session() as session:
        job = Job(
            url=test_url,
            title="Senior Python Developer",
            company="Acme Corp",
            source="linkedin",
            description="Looking for an expert Python dev.",
            status="discovered"
        )
        session.add(job)
        # Session auto-commits on context exit

    # 3. Read & Verify Job, then Insert Application
    with get_db_session() as session:
        stmt = select(Job).filter_by(url=test_url)
        job = session.execute(stmt).scalar_one_or_none()
        
        assert job is not None
        assert job.title == "Senior Python Developer"
        assert job.company == "Acme Corp"
        assert job.status == "discovered"
        assert job.score is None

        # Add Application referencing the Job
        app = Application(
            job_id=job.id,
            tailored_resume_path="/paths/resume.txt",
            cover_letter_path="/paths/cover.txt"
        )
        session.add(app)

    # 4. Verify Application relationship and cascade
    with get_db_session() as session:
        # Fetch Job and verify it loads application relation
        stmt = select(Job).filter_by(url=test_url)
        job = session.execute(stmt).scalar_one_or_none()
        
        assert job is not None
        assert job.application is not None
        assert job.application.tailored_resume_path == "/paths/resume.txt"

        # Modify Job status and score
        job.status = "scored"
        job.score = 8.5
        job.reasoning = "Strong Python background."

    # Verify updates
    with get_db_session() as session:
        stmt = select(Job).filter_by(url=test_url)
        job = session.execute(stmt).scalar_one_or_none()
        assert job.status == "scored"
        assert job.score == 8.5
        assert job.reasoning == "Strong Python background."

    # 5. Insert RunLog
    with get_db_session() as session:
        log = RunLog(
            stage=test_stage,
            jobs_processed=1
        )
        session.add(log)

    with get_db_session() as session:
        stmt = select(RunLog).filter_by(stage=test_stage)
        log = session.execute(stmt).scalar_one_or_none()
        assert log is not None
        assert log.jobs_processed == 1

    # 6. Clean up (Verify Cascade Delete)
    with get_db_session() as session:
        stmt = select(Job).filter_by(url=test_url)
        job = session.execute(stmt).scalar_one_or_none()
        session.delete(job)
        
        stmt_log = select(RunLog).filter_by(stage=test_stage)
        log = session.execute(stmt_log).scalar_one_or_none()
        session.delete(log)

    # 7. Final check to ensure records are removed
    with get_db_session() as session:
        job = session.execute(select(Job).filter_by(url=test_url)).scalar_one_or_none()
        app = session.execute(select(Application).filter_by(tailored_resume_path="/paths/resume.txt")).scalar_one_or_none()
        log = session.execute(select(RunLog).filter_by(stage=test_stage)).scalar_one_or_none()
        
        assert job is None
        assert app is None
        assert log is None
