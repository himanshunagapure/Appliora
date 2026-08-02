import logging
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger("jobapplypro.retry")

def _log_retry(retry_state):
    """
    Log retry attempts safely. next_action is None on the final attempt
    (when no further sleep is scheduled), so we guard against AttributeError.
    """
    sleep_secs = getattr(retry_state.next_action, "sleep", None)
    sleep_str = f"{sleep_secs:.1f}s" if sleep_secs is not None else "final attempt"
    func_name = retry_state.fn.__name__ if retry_state.fn else "unknown"
    logger.warning(
        f"Attempt {retry_state.attempt_number} failed for '{func_name}'. "
        f"Retrying in {sleep_str}..."
    )

def retry_scraping(func):
    """
    Decorator that applies exponential backoff retries.
    Retries up to 3 times, with a backoff multiplier of 2, starting at 2s up to 30s.
    """
    return retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        before_sleep=_log_retry,
    )(func)
