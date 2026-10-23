"""
Prompt builder for Phase 5 resume tailoring. Kept separate from core/tailor.py
so the prompt can be iterated on without touching orchestration logic.
"""


def build_tailor_prompt(
    profile_json: str,
    resume_facts: list[str],
    job_title: str,
    company: str,
    job_description: str,
    validation_errors: list[str] | None = None,
) -> str:
    facts_block = "\n".join(f"- {fact}" for fact in resume_facts)
    retry_block = ""
    if validation_errors:
        missing = "\n".join(f"- {e}" for e in validation_errors)
        retry_block = f"""

Your previous tailored resume FAILED fact-preservation validation. These protected
resume_facts were missing or altered:
{missing}

Regenerate the resume. For each missing fact, the content after the label
MUST appear character-for-character as a substring in the tailored resume.
"""

    return f"""You are rewriting a candidate's resume for ONE specific job application.

Hard constraints (non-negotiable):
1. NEVER invent employers, titles, dates, degrees, projects, metrics, or skills
   that are not in the candidate profile.
2. NEVER alter protected resume_facts. For each fact below, the content AFTER
   the label (e.g. for "Company: Acme Corp" the string "Acme Corp") MUST appear
   as an exact substring in your tailored resume.
3. You MAY reorder sections, emphasize relevant bullets, trim less-relevant
   bullets, and mirror ATS keywords from the job description — but only when
   those keywords already map to real experience in the profile.
4. Output an ATS-friendly resume in markdown (single column). Use ONLY these
   section headings, in this order when the profile has content for them:
     # Full Name
     City, Region | phone | email | LinkedIn or GitHub as plain text (no markdown links)
     ## Summary
     ## Skills
     ## Experience
     ### Job Title | Company | Mon YYYY – Mon YYYY (or YYYY)
     - achievement bullets
     ## Projects
     ## Education
     ## Certifications
5. ATS layout rules: no tables, no columns, no images, no icons, no text boxes.
   Spell out skill names. Do not use headers/footers. Dates as Mon YYYY or YYYY.
6. Include ALL experience achievements, projects, education entries, and skills
   from the profile — do not drop protected fact content to shorten the resume.

PROTECTED resume_facts (must appear unchanged as substrings):
{facts_block}

CANDIDATE PROFILE (JSON):
{profile_json}

TARGET JOB:
Title: {job_title}
Company: {company}
Description:
{job_description}
{retry_block}
Respond with ONLY this JSON object, no markdown fences, no commentary:
{{
  "tailored_resume": "<full ATS-friendly markdown resume text>",
  "emphasis_notes": "<1-2 sentences on what you emphasized for this job>"
}}"""
