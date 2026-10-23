"""
Phase 4 — Scoring (FR3)

For every job in status="discovered": combine an embedding-based semantic
similarity check against the candidate profile with an LLM judgment pass,
producing a single 1-10 fit score + reasoning. Jobs at/above
settings.score_threshold are the ones worth spending tailoring effort on.

Public entry point: run_scoring_stage()
"""

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config.settings import get_settings
from core.experience import candidate_years_of_experience, experience_gate
from core.location import location_preferences, location_score_adjustment
from db.models import Job, RunLog
from db.session import get_db_session
from llm.client import EmbeddingClient, LLMClient, LLMError
from llm.prompts.score_prompt import build_score_prompt
from utils.logger import get_logger

logger = get_logger("jobapplypro.score")


class ScoringError(RuntimeError):
    """Raised when a single job can't be scored (caught per-job by the stage runner)."""


# ---------------------------------------------------------------------------
# 1. Profile summary + embedding (computed once per run, cached across runs)
# ---------------------------------------------------------------------------

def load_profile(profile_path: str | Path | None = None) -> dict[str, Any]:
    settings = get_settings()
    path = Path(profile_path or settings.profile_output_path)
    if not path.exists():
        raise ScoringError(
            f"No profile found at {path}. Run `python cli.py init --resume <path>` first."
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_profile_summary(profile: dict[str, Any]) -> str:
    """
    Condenses the structured profile into a text blob used for both the
    embedding and the LLM prompt. Deliberately excludes contact info (not
    relevant to fit) and keeps resume_facts out (that list is for tailor.py's
    fact-preservation check, not for scoring context).
    """
    lines: list[str] = []

    years = candidate_years_of_experience(profile)
    lines.append(f"Years of experience: {years:g} years (from resume dates)")

    prefs = location_preferences(profile)
    lines.append(
        "Location preference (soft, not a knockout): " + " / ".join(prefs)
    )

    skills = profile.get("skills", [])
    if skills:
        lines.append("Skills: " + ", ".join(skills))

    for exp in profile.get("experience", []):
        title = exp.get("title", "")
        company = exp.get("company", "")
        dates = f"{exp.get('start_date', '')} - {exp.get('end_date', '')}"
        achievements = "; ".join(exp.get("achievements", []))
        lines.append(f"{title} at {company} ({dates}): {achievements}")

    for proj in profile.get("projects", []):
        name = proj.get("name", "")
        tech = ", ".join(proj.get("technologies", []))
        achievements = "; ".join(proj.get("achievements", []))
        lines.append(f"Project '{name}' [{tech}]: {achievements}")

    summary = profile.get("summary")
    if summary:
        lines.insert(0, summary)

    return "\n".join(lines)


def get_profile_embedding(
    profile_summary: str,
    embedding_client: EmbeddingClient,
    cache_path: str | Path | None = None,
) -> list[float]:
    """
    Caches the profile embedding on disk, keyed by a hash of the summary text,
    so re-running the scoring stage doesn't re-embed an unchanged profile
    every time. Cache is automatically invalidated if profile.json changes.
    """
    settings = get_settings()
    cache_path = Path(cache_path or settings.profile_embedding_cache_path)
    summary_hash = hashlib.sha256(profile_summary.encode("utf-8")).hexdigest()

    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("summary_hash") == summary_hash:
                logger.info("Using cached profile embedding (hash matches).")
                return cached["embedding"]
        except (json.JSONDecodeError, KeyError):
            logger.warning("Profile embedding cache was unreadable; recomputing.")

    logger.info("Computing profile embedding (no valid cache found).")
    embedding = embedding_client.embed_text(profile_summary)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"summary_hash": summary_hash, "embedding": embedding}), encoding="utf-8"
    )
    return embedding


# ---------------------------------------------------------------------------
# 2. Similarity + score combination
# ---------------------------------------------------------------------------

def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ScoringError(f"Embedding dimension mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def combine_score(semantic_similarity: float, llm_score: float, settings=None) -> float:
    """Blend semantic similarity (0-1, scaled to 0-10) with the LLM's 1-10 judgment."""
    settings = settings or get_settings()
    semantic_scaled = max(0.0, min(1.0, semantic_similarity)) * 10
    combined = (
        settings.score_semantic_weight * semantic_scaled
        + settings.score_llm_weight * llm_score
    )
    return round(max(0.0, min(10.0, combined)), 1)


# ---------------------------------------------------------------------------
# 3. Per-job scoring
# ---------------------------------------------------------------------------

def score_job(
    job_title: str,
    company: str,
    job_description: str,
    profile_summary: str,
    profile_embedding: list[float],
    embedding_client: EmbeddingClient,
    llm_client: LLMClient,
    profile: dict[str, Any] | None = None,
) -> tuple[float, str]:
    """Returns (final_score, reasoning_text) for a single job."""
    job_text = f"{job_title} at {company}\n{job_description or ''}"

    try:
        job_embedding = embedding_client.embed_text(job_text)
        semantic_similarity = cosine_similarity(profile_embedding, job_embedding)
    except Exception as exc:
        raise ScoringError(f"Embedding step failed: {exc}") from exc

    prompt = build_score_prompt(
        profile_summary=profile_summary,
        job_title=job_title,
        company=company,
        job_description=job_description or "",
        semantic_similarity=semantic_similarity,
    )

    try:
        result = llm_client.generate_json(prompt)
    except LLMError as exc:
        raise ScoringError(f"LLM scoring step failed: {exc}") from exc

    try:
        llm_score = float(result["score"])
        llm_reasoning = str(result.get("reasoning", "")).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise ScoringError(f"LLM returned an unexpected score shape: {result}") from exc

    llm_score = max(1.0, min(10.0, llm_score))
    final_score = combine_score(semantic_similarity, llm_score)
    loc_delta, loc_note = location_score_adjustment(
        profile, job_title, job_description or ""
    )
    if loc_delta:
        final_score = round(max(0.0, min(10.0, final_score + loc_delta)), 1)
    reasoning = (
        f"Semantic similarity: {semantic_similarity:.2f} | LLM judged {llm_score:.0f}/10 "
        f"| {llm_reasoning} | {loc_note}"
    )
    return final_score, reasoning


# ---------------------------------------------------------------------------
# 4. Stage orchestration
# ---------------------------------------------------------------------------

def run_scoring_stage(
    session_scope: Callable | None = None,
    embedding_client: EmbeddingClient | None = None,
    llm_client: LLMClient | None = None,
    profile_path: str | Path | None = None,
) -> dict[str, int]:
    """
    Scores every job currently in status="discovered".
    Each job is scored and committed independently — one bad job (malformed
    description, LLM hiccup) is logged and marked "failed" without stopping
    the rest of the batch. Returns a summary dict for the CLI to print.
    """
    settings = get_settings()
    session_scope = session_scope or get_db_session
    embedding_client = embedding_client or EmbeddingClient()
    llm_client = llm_client or LLMClient()

    profile = load_profile(profile_path)
    profile_summary = build_profile_summary(profile)
    profile_embedding = get_profile_embedding(profile_summary, embedding_client)

    summary = {
        "processed": 0,
        "scored": 0,
        "above_threshold": 0,
        "failed": 0,
        "skipped": 0,
    }
    error_messages: list[str] = []
    high_score_jobs: list[dict[str, Any]] = []

    with session_scope() as session:
        run_log = RunLog(stage="score", jobs_processed=0)
        session.add(run_log)
        session.flush()  # get run_log.id without committing yet

        discovered_jobs = session.query(Job).filter(Job.status == "discovered").all()
        logger.info("Scoring %d discovered job(s).", len(discovered_jobs))

        for job in discovered_jobs:
            summary["processed"] += 1
            qualified, yoe_reason = experience_gate(
                profile, job.title or "", job.description or ""
            )
            if not qualified:
                job.status = "skipped"
                job.reasoning = yoe_reason
                job.score = 0.0
                summary["skipped"] += 1
                logger.info(
                    "Skipped job %d (%s at %s): %s",
                    job.id,
                    job.title,
                    job.company,
                    yoe_reason,
                )
                session.commit()
                continue
            try:
                score, reasoning = score_job(
                    job_title=job.title,
                    company=job.company,
                    job_description=job.description,
                    profile_summary=profile_summary,
                    profile_embedding=profile_embedding,
                    embedding_client=embedding_client,
                    llm_client=llm_client,
                    profile=profile,
                )
                job.score = score
                job.reasoning = f"{yoe_reason} | {reasoning}"
                job.status = "scored"
                summary["scored"] += 1
                if score >= settings.score_threshold:
                    summary["above_threshold"] += 1
                    high_score_jobs.append(
                        {
                            "id": job.id,
                            "title": job.title,
                            "company": job.company,
                            "score": score,
                            "url": job.url,
                            "source": job.source,
                        }
                    )
                logger.info("Scored job %d (%s at %s): %.1f", job.id, job.title, job.company, score)
            except ScoringError as exc:
                job.status = "failed"
                job.reasoning = f"Scoring failed: {exc}"
                summary["failed"] += 1
                error_messages.append(f"job {job.id}: {exc}")
                logger.error("Failed to score job %d (%s): %s", job.id, job.title, exc)

            session.commit()  # durable per-job: a crash on job N+1 doesn't lose job N

        run_log.jobs_processed = summary["processed"]
        run_log.finished_at = datetime.now(timezone.utc)
        run_log.errors = "\n".join(error_messages) if error_messages else None
        session.commit()

    if high_score_jobs:
        try:
            from core.notify import notify_high_score_jobs

            notify_high_score_jobs(high_score_jobs)
        except Exception as exc:
            logger.warning("High-score notification failed: %s", exc)

    logger.info("Scoring stage complete: %s", summary)
    return summary
