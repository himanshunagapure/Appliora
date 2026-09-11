"""
Prompt builder for Phase 4 scoring. Kept separate from core/score.py so the
prompt can be iterated on without touching orchestration logic.
"""


def build_score_prompt(
    profile_summary: str,
    job_title: str,
    company: str,
    job_description: str,
    semantic_similarity: float,
) -> str:
    return f"""You are a strict technical recruiter deciding whether a job is a good
fit for a candidate. Score honestly — most jobs are NOT a great fit, and inflating
scores wastes the candidate's time on applications that will be rejected.

CANDIDATE PROFILE:
{profile_summary}

JOB POSTING:
Title: {job_title}
Company: {company}
Description:
{job_description}

An embedding-based semantic similarity check already scored this pair at
{semantic_similarity:.2f} (0.0 = unrelated, 1.0 = near-identical). Use this as a
signal, but judge independently — semantic similarity can be misleadingly high
for jobs that share vocabulary but differ in seniority, domain, or role type.

Score this job from 1 to 10 on fit for this specific candidate, considering:
- Do their skills and experience actually match what's required (not just adjacent)?
- Is the seniority level appropriate (not obviously over/under-qualified)?
- Is this the kind of role/domain they've actually worked in?

Respond with ONLY this JSON object, no markdown fences, no commentary:
{{
  "score": <integer 1-10>,
  "reasoning": "<2-3 sentences explaining the score, specific to this job and this candidate>"
}}"""
