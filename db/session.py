from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

from config.settings import get_settings
from utils.logger import get_logger

logger = get_logger("jobapplypro.db")

# Get settings to obtain the configured DATABASE_URL
settings = get_settings()

# Configure the SQLAlchemy engine
# pool_pre_ping=True helps detect stale connections (especially useful for postgres)
engine = create_engine(settings.database_url, pool_pre_ping=True)


@event.listens_for(Engine, "connect")
def _sqlite_register_now(dbapi_connection, connection_record) -> None:
    """
    Alembic migrations use server_default=now() (Postgres). SQLite has no
    built-in now(); register a compatible function so sqlite:/// URLs work
    for local / cron-friendly runs without Postgres.
    """
    module = type(dbapi_connection).__module__
    name = type(dbapi_connection).__name__
    if "sqlite" not in module and name != "Connection":
        return
    try:
        import sqlite3

        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
    except Exception:
        return

    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    dbapi_connection.create_function("now", 0, _now)

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
