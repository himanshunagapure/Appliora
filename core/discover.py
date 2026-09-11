import datetime
import math
from typing import Any

import pandas as pd
from jobspy import scrape_jobs

from config.settings import get_settings
from db.session import get_db_session
from db.models import Job, RunLog
from utils.retry import retry_scraping
from utils.logger import setup_logger, log_transition

logger = setup_logger("jobapplypro.discover")


def _clean_text(value: Any, default: str = "") -> str:
    """
    Coerce JobSpy/pandas cell values to a clean string.
    pandas uses NaN for missing fields; `str(nan)` becomes the literal 'nan',
    which we must never store as a company name or job description.
    """
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and math.isnan(value):
        return default
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null", "nat"}:
        return default
    return text


@retry_scraping
def scrape_jobs_with_retry(**kwargs):
    """
    Wrap JobSpy's scrape_jobs inside tenacity's retry mechanism.
    """
    logger.info(f"Calling JobSpy with parameters: {kwargs}")
    return scrape_jobs(**kwargs)


def run_discovery():
    """
    Orchestrates the discovery phase. Reads searches from settings,
    scrapes each configured query, filters duplicates, maps fields,
    and commits new listings to the database.
    """
    settings = get_settings()

    # Initialize run log
    run_log = RunLog(
        stage="discover",
        started_at=datetime.datetime.now(datetime.timezone.utc),
    )

    with get_db_session() as session:
        session.add(run_log)
        session.flush()  # flush to get the auto-generated ID without committing yet
        run_log_id = run_log.id
        # session auto-commits on context exit

    total_processed = 0
    total_inserted = 0
    total_updated = 0
    errors_occurred = []

    for search in settings.searches:
        logger.info(f"Processing search role: '{search.role}'")

        for location in search.locations:
            logger.info(
                f"Scraping '{search.role}' in '{location}' on sites {search.site_names}"
            )

            try:
                # linkedin_fetch_description=True is required: LinkedIn search cards
                # do not include the body; JobSpy must open each job page for text.
                df = scrape_jobs_with_retry(
                    site_name=search.site_names,
                    search_term=search.role,
                    location=location,
                    results_wanted=search.results_wanted,
                    hours_old=search.hours_old,
                    is_remote=search.remote_only,
                    linkedin_fetch_description=True,
                )

                if df is None or df.empty:
                    logger.info(
                        f"No jobs returned for '{search.role}' in '{location}'"
                    )
                    continue

                logger.info(
                    f"Scraped {len(df)} jobs for '{search.role}' in '{location}'"
                )

                with get_db_session() as session:
                    for _, row in df.iterrows():
                        total_processed += 1

                        # Job URL is required for deduplication and application
                        url = _clean_text(row.get("job_url") or row.get("job_url_direct"))
                        if not url:
                            continue

                        title = _clean_text(row.get("title"), default="Unknown Title")
                        company = _clean_text(
                            row.get("company"), default="Unknown Company"
                        )
                        source = _clean_text(row.get("site"), default="unknown").lower()
                        description_base = _clean_text(row.get("description"))

                        # Capture Naukri-specific metadata if scraping from Naukri
                        extra_metadata = []
                        if source == "naukri":
                            skills = _clean_text(row.get("skills"))
                            if skills:
                                extra_metadata.append(f"Skills: {skills}")

                            exp = _clean_text(
                                row.get("experience_range") or row.get("experience")
                            )
                            if exp:
                                extra_metadata.append(f"Experience Range: {exp}")

                            rating = _clean_text(row.get("company_rating"))
                            if rating:
                                extra_metadata.append(f"Company Rating: {rating}")

                            wfh = _clean_text(row.get("work_from_home_type"))
                            if wfh:
                                extra_metadata.append(f"Work From Home: {wfh}")

                        if extra_metadata:
                            metadata_section = (
                                "\n\n--- Naukri Specific Info ---\n"
                                + "\n".join(extra_metadata)
                            )
                            description = description_base + metadata_section
                            logger.info(
                                f"Extracted Naukri metadata for job: {title} at {company}"
                            )
                        else:
                            description = description_base

                        if not description:
                            logger.warning(
                                "Job has empty description after scrape "
                                f"({source}): {title} at {company} — {url}"
                            )

                        # If we already have the URL but stored an empty/"nan"
                        # description (common for LinkedIn before fetch was enabled),
                        # backfill when JobSpy now returns real text.
                        existing = session.query(Job).filter(Job.url == url).one_or_none()
                        if existing is not None:
                            if description and not _clean_text(existing.description):
                                existing.description = description
                                if existing.company in {"", "nan", "Unknown Company"} and company != "Unknown Company":
                                    existing.company = company
                                # Re-score once we have real text.
                                if existing.status in {"scored", "failed"}:
                                    existing.status = "discovered"
                                    existing.score = None
                                    existing.reasoning = None
                                total_updated += 1
                                logger.info(
                                    f"Backfilled description for existing job id={existing.id} ({source})"
                                )
                            else:
                                logger.debug(f"Skipping duplicate job URL: {url}")
                            continue

                        new_job = Job(
                            url=url,
                            title=title,
                            company=company,
                            source=source,
                            description=description or None,
                            status="discovered",
                        )
                        session.add(new_job)
                        total_inserted += 1

                        log_transition(
                            logger=logger,
                            job_id="NEW",
                            stage="discover",
                            from_status="none",
                            to_status="discovered",
                            message=f"{title} at {company} ({source})",
                        )
            except Exception as e:
                err_msg = f"Failed scraping '{search.role}' in '{location}': {str(e)}"
                logger.error(err_msg, exc_info=True)
                errors_occurred.append(err_msg)

    # Save details into RunLog
    with get_db_session() as session:
        db_run_log = session.get(RunLog, run_log_id)
        if db_run_log:
            db_run_log.finished_at = datetime.datetime.now(datetime.timezone.utc)
            db_run_log.jobs_processed = total_inserted + total_updated
            if errors_occurred:
                db_run_log.errors = "\n".join(errors_occurred)
            session.add(db_run_log)

    logger.info(
        f"Discovery run complete. Processed {total_processed} jobs, "
        f"inserted {total_inserted} new, backfilled {total_updated} descriptions."
    )
