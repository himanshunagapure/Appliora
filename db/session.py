from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from config.settings import get_settings

# Get settings to obtain the configured DATABASE_URL
settings = get_settings()

# Configure the SQLAlchemy engine
# pool_pre_ping=True helps detect stale connections (especially useful for postgres)
engine = create_engine(settings.database_url, pool_pre_ping=True)

# Session factory configuration
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """
    Provide a transactional scope around a series of operations.
    Ensures the session is automatically committed on success,
    rolled back on error, and closed after usage.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
