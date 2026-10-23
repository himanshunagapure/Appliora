"""
Ensure the SQLAlchemy schema is at Alembic head.

Old databases created before Phase 8 lack `applications.review_decision` /
`reviewed_at`. Calling `ensure_schema()` (from the CLI DB guard) upgrades
automatically so operators do not have to remember `alembic upgrade head`.
"""

from __future__ import annotations

from pathlib import Path

from utils.logger import get_logger

logger = get_logger("jobapplypro.migrate")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def ensure_schema(*, alembic_ini: Path | None = None) -> None:
    """
    Run `alembic upgrade head` against the configured DATABASE_URL.

    Idempotent — no-op when already at head. Raises on failure so callers
    (pipeline / CLI) can abort cleanly.
    """
    from alembic import command
    from alembic.config import Config

    from config.settings import get_settings

    ini = Path(alembic_ini or ALEMBIC_INI)
    if not ini.is_file():
        raise RuntimeError(f"alembic.ini not found at {ini}")

    settings = get_settings()
    cfg = Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

    logger.info("Ensuring database schema is at Alembic head…")
    command.upgrade(cfg, "head")
    logger.info("Database schema is up to date")
