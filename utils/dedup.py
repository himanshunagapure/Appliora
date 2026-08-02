from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import Job

def is_job_exists(session: Session, url: str) -> bool:
    """
    Checks if a job with the specified URL already exists in the database.
    Returns True if it exists, False otherwise.
    Uses SQLAlchemy 2.0 style where() for correctness on column-only selects.
    """
    stmt = select(Job.id).where(Job.url == url)
    result = session.execute(stmt).first()
    return result is not None
