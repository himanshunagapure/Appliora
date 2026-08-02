import os
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional

def get_logger(name: str = "jobapplypro", log_level: int = logging.INFO) -> logging.Logger:
    """Alias used by newer modules; same as setup_logger."""
    return setup_logger(name, log_level=log_level)


def setup_logger(name: str = "jobapplypro", log_level: int = logging.INFO) -> logging.Logger:
    """
    Sets up and configures the logger. Adds a StreamHandler for the console
    and a RotatingFileHandler to write logs into logs/jobapplypro.log.
    """
    logger = logging.getLogger(name)
    logger.setLevel(log_level)
    
    # Prevent adding handlers multiple times
    if logger.handlers:
        return logger

    # Ensure the logs directory exists
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(project_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "jobapplypro.log")

    # Define standard format
    # Example format: 2026-08-02 13:10:00 - core.discover - INFO - Discovery started
    formatter = logging.Formatter(
        fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Rotating File Handler (10 MB per file, up to 5 backups)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def log_transition(
    logger: logging.Logger, 
    job_id: str, 
    stage: str, 
    from_status: str, 
    to_status: str, 
    message: Optional[str] = None
):
    """
    Helper to log job status transitions in a structured format.
    Format: [Stage: discover] [Job: 123] [Transition: discovered -> scored] - Optional details
    """
    log_msg = f"[Stage: {stage}] [Job: {job_id}] [Transition: {from_status} -> {to_status}]"
    if message:
        log_msg += f" - {message}"
    logger.info(log_msg)
