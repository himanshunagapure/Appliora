import json
import math
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.score import (
    ScoringError,
    build_profile_summary,
    combine_score,
    cosine_similarity,
    get_profile_embedding,
    run_scoring_stage,
    score_job,
)
from db.models import Base, Job

SAMPLE_PROFILE = json.loads((Path(__file__).parent / "fixtures" / "sample_profile.json").read_text())


class FakeEmbeddingClient:
    """
    Deterministic, network-free stand-in for llm.client.EmbeddingClient.
    Produces different vectors for different text so cosine similarity is
    meaningful in tests, without downloading any real model.
    """

    def embed_text(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            # Hash-derived, stable per unique string, cheap, no network.
            seed = sum(ord(c) for c in text)
            vectors.append([math.sin(seed + i) for i in range(8)])
        return vectors


class FakeLLMClient:
    def __init__(self, responses):
        self._responses = list(responses)

    def generate_json(self, prompt, max_retries=None):
        return self._responses.pop(0)


def make_temp_sqlite_session_scope():
    """
    Returns a session_scope() context manager bound to one shared in-memory
    SQLite engine, so multiple `with` blocks in the same test see the same
    data (a fresh :memory: engine per call would give each block its own
    empty DB).
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    @contextmanager
    def session_scope():
        session = SessionLocal()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    return session_scope


# ---------------------------------------------------------------------------
# Pure math / logic
# ---------------------------------------------------------------------------

def test_cosine_similarity_identical_vectors_is_one():
    v = [1.0, 2.0, 3.0]
    assert cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors_is_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_dimension_mismatch_raises():
    with pytest.raises(ScoringError):
        cosine_similarity([1.0, 2.0], [1.0])


def test_combine_score_weights_semantic_and_llm():
    from config.settings import Settings

    fake_settings = Settings(
        gemini_api_key="test",
        score_semantic_weight=0.5,
        score_llm_weight=0.5,
    )
    # semantic_similarity=1.0 -> scaled 10; llm_score=6 -> combined = 0.5*10 + 0.5*6 = 8.0
    assert combine_score(1.0, 6.0, settings=fake_settings) == pytest.approx(8.0)


def test_combine_score_clamped_to_0_10_range():
    from config.settings import Settings

    fake_settings = Settings(
        gemini_api_key="test",
        score_semantic_weight=1.0,
        score_llm_weight=1.0,
    )
    assert combine_score(1.0, 10.0, settings=fake_settings) == 10.0


def test_build_profile_summary_includes_skills_and_experience():
    summary = build_profile_summary(SAMPLE_PROFILE)
    assert "LangGraph" in summary
    assert "AIQoD" in summary
    assert "AI/ML Developer" in summary


def test_build_profile_summary_excludes_contact_and_resume_facts():
    summary = build_profile_summary(SAMPLE_PROFILE)
    assert SAMPLE_PROFILE["contact"]["email"] not in summary


# ---------------------------------------------------------------------------
# Embedding cache
# ---------------------------------------------------------------------------

def test_profile_embedding_is_cached_and_reused(tmp_path):
    cache_path = tmp_path / "cache.json"
    client = FakeEmbeddingClient()

    first = get_profile_embedding("some profile text", client, cache_path=cache_path)
    assert cache_path.exists()

    # Swap in a client that would raise if actually called again — proves the cache hit.
    class ExplodingClient:
        def embed_text(self, text):
            raise AssertionError("Should not recompute — cache should have been used.")

    second = get_profile_embedding("some profile text", ExplodingClient(), cache_path=cache_path)
    assert first == second


def test_profile_embedding_cache_invalidated_on_text_change(tmp_path):
    cache_path = tmp_path / "cache.json"
    client = FakeEmbeddingClient()

    get_profile_embedding("profile version A", client, cache_path=cache_path)
    # Different text -> different hash -> cache miss -> recomputed, no error.
    result = get_profile_embedding("profile version B", client, cache_path=cache_path)
    assert result == client.embed_text("profile version B")


# ---------------------------------------------------------------------------
# Per-job scoring
# ---------------------------------------------------------------------------

def test_score_job_combines_semantic_and_llm_score():
    llm_client = FakeLLMClient(responses=[{"score": 8, "reasoning": "Strong GenAI overlap."}])
    embedding_client = FakeEmbeddingClient()
    profile_summary = build_profile_summary(SAMPLE_PROFILE)
    profile_embedding = embedding_client.embed_text(profile_summary)

    score, reasoning = score_job(
        job_title="GenAI Developer",
        company="Some Startup",
        job_description="Looking for LangGraph and RAG experience.",
        profile_summary=profile_summary,
        profile_embedding=profile_embedding,
        embedding_client=embedding_client,
        llm_client=llm_client,
    )

    assert 1.0 <= score <= 10.0
    assert "Strong GenAI overlap" in reasoning
    assert "Semantic similarity" in reasoning


def test_score_job_raises_scoring_error_on_malformed_llm_response():
    llm_client = FakeLLMClient(responses=[{"unexpected_key": "oops"}])
    embedding_client = FakeEmbeddingClient()
    profile_summary = build_profile_summary(SAMPLE_PROFILE)
    profile_embedding = embedding_client.embed_text(profile_summary)

    with pytest.raises(ScoringError):
        score_job(
            job_title="Anything",
            company="Anything",
            job_description="Anything",
            profile_summary=profile_summary,
            profile_embedding=profile_embedding,
            embedding_client=embedding_client,
            llm_client=llm_client,
        )


# ---------------------------------------------------------------------------
# Full stage: run_scoring_stage against a real (temp) SQLite DB
# ---------------------------------------------------------------------------

def test_run_scoring_stage_scores_all_discovered_jobs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "profile.json").write_text(json.dumps(SAMPLE_PROFILE))

    session_scope = make_temp_sqlite_session_scope()

    # Seed jobs directly via the temp session before scoring runs.
    with session_scope() as seed_session:
        seed_session.add_all(
            [
                Job(url="https://x.com/1", title="GenAI Developer", company="A", source="indeed",
                    description="LangGraph, RAG, multi-agent systems.", status="discovered"),
                Job(url="https://x.com/2", title="Warehouse Associate", company="B", source="indeed",
                    description="Lift boxes, operate forklift.", status="discovered"),
            ]
        )

    llm_client = FakeLLMClient(
        responses=[
            {"score": 9, "reasoning": "Excellent match on GenAI stack."},
            {"score": 1, "reasoning": "Completely unrelated field."},
        ]
    )
    embedding_client = FakeEmbeddingClient()

    summary = run_scoring_stage(
        session_scope=session_scope,
        embedding_client=embedding_client,
        llm_client=llm_client,
        profile_path=tmp_path / "config" / "profile.json",
    )

    assert summary["processed"] == 2
    assert summary["scored"] == 2
    assert summary["failed"] == 0

    with session_scope() as session:
        jobs = session.query(Job).order_by(Job.id).all()
        assert all(j.status == "scored" for j in jobs)
        assert jobs[0].score > jobs[1].score  # GenAI job should outscore warehouse job


def test_run_scoring_stage_isolates_a_failing_job(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "profile.json").write_text(json.dumps(SAMPLE_PROFILE))

    session_scope = make_temp_sqlite_session_scope()

    with session_scope() as seed_session:
        seed_session.add_all(
            [
                Job(url="https://x.com/good", title="GenAI Developer", company="A", source="indeed",
                    description="LangGraph.", status="discovered"),
                Job(url="https://x.com/bad", title="Broken Job", company="B", source="indeed",
                    description="Will fail scoring.", status="discovered"),
            ]
        )

    # First job scores fine; second gets a malformed LLM response and should be
    # isolated as "failed" without stopping the batch.
    llm_client = FakeLLMClient(
        responses=[
            {"score": 7, "reasoning": "Good fit."},
            {"totally": "wrong shape"},
        ]
    )
    embedding_client = FakeEmbeddingClient()

    summary = run_scoring_stage(
        session_scope=session_scope,
        embedding_client=embedding_client,
        llm_client=llm_client,
        profile_path=tmp_path / "config" / "profile.json",
    )

    assert summary["processed"] == 2
    assert summary["scored"] == 1
    assert summary["failed"] == 1

    with session_scope() as session:
        statuses = {j.url: j.status for j in session.query(Job).all()}
        assert statuses["https://x.com/good"] == "scored"
        assert statuses["https://x.com/bad"] == "failed"
