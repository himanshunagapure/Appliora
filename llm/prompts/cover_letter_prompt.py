"""
Prompt builder for Phase 6 cover letter generation (FR5).
Kept separate from core/cover_letter.py so the prompt can be iterated on
without touching orchestration logic.
"""


def build_cover_letter_prompt(
    profile_json: str,
    resume_facts: list[str],
    job_title: str,
    company: str,
    job_description: str,
    tailored_resume: str | None = None,
    validation_errors: list[str] | None = None,
) -> str:
    facts_block = "\n".join(f"- {fact}" for fact in resume_facts)
    tailored_block = ""
    if tailored_resume:
        tailored_block = f"""
TAILORED RESUME (already written for this job — ground the letter in the same facts):
{tailored_resume}
"""

    retry_block = ""
    if validation_errors:
        problems = "\n".join(f"- {e}" for e in validation_errors)
        retry_block = f"""

Your previous cover letter FAILED validation:
{problems}

Regenerate the letter. Fix every listed problem. The company name and job title
must appear as exact substrings (case may vary). Do not invent new experience.
"""

    return f"""You are writing a short, professional cover letter for ONE job application.

Hard constraints (non-negotiable):
1. NEVER invent employers, titles, dates, degrees, projects, metrics, skills,
   or achievements that are not in the candidate profile / resume_facts /
   tailored resume below.
2. Reference the company and role explicitly — the exact company name
   "{company}" and the exact job title "{job_title}" MUST appear in the letter.
3. Keep it short: 3–4 paragraphs, roughly 200–350 words. No fluff, no clichés
   like "I am writing to express my interest" as the opener if you can avoid it.
4. Ground claims in real experience from the profile (prefer concrete projects,
   tech, and outcomes that map to the job description).
5. Plain text only — no markdown headings, no bullet lists, no JSON inside the
   letter body. Sign off with the candidate's real name from the profile.
6. Do not claim years of experience you cannot support from the profile dates.

PROTECTED resume_facts (ground truth — do not contradict or invent beyond these):
{facts_block}

CANDIDATE PROFILE (JSON):
{profile_json}
{tailored_block}
TARGET JOB:
Title: {job_title}
Company: {company}
Description:
{job_description}
{retry_block}
Respond with ONLY this JSON object, no markdown fences, no commentary:
{{
  "cover_letter": "<full plain-text cover letter>",
  "emphasis_notes": "<1 sentence on which experience you highlighted>"
}}"""
