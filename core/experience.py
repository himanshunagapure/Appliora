"""
Years-of-experience gate (mandatory before qualifying a job).

Candidate years are computed from profile experience dates (resume).
Job postings are parsed for an explicit years requirement:

  1. JD specifies years → qualify only when candidate years match.
  2. JD does not specify years → qualify (continue scoring as usual).
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from utils.logger import get_logger

logger = get_logger("jobapplypro.experience")

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

# Company-age / history phrasing — not a candidate requirement.
_EXCLUDE = re.compile(
    r"\b(ago|old|founded|established|history|anniversary|since\s+\d{4})\b",
    re.I,
)

# Academic duration, not professional YoE.
_ACADEMIC = re.compile(
    r"\b(degree|bachelor|master|university|college|phd|mba|program)\b",
    re.I,
)

_YEARS_UNIT = r"(?:years?|yrs?)"


@dataclass(frozen=True)
class ExperienceRequirement:
    min_years: float
    max_years: float | None
    raw: str
    start: int = 0
    end: int = 0

    def describes(self) -> str:
        if self.max_years is None:
            return f"{self.min_years:g}+ years"
        if self.min_years == self.max_years:
            return f"{self.min_years:g} years"
        return f"{self.min_years:g}–{self.max_years:g} years"

    def is_range(self) -> bool:
        return self.max_years is not None and self.max_years != self.min_years


def candidate_years_of_experience(
    profile: dict[str, Any],
    *,
    as_of: date | None = None,
) -> float:
    """
    Total years across profile experience entries (resume dates).

    Overlapping jobs are counted once (union of months). Missing dates are
    skipped. 'Present' / 'current' uses as_of (default: today).
    """
    as_of = as_of or date.today()
    months: set[tuple[int, int]] = set()
    for job in profile.get("experience") or []:
        if not isinstance(job, dict):
            continue
        start = _parse_date(job.get("start_date"), end_of_month=False)
        end_raw = job.get("end_date")
        if _is_present(end_raw):
            end = as_of
        else:
            end = _parse_date(end_raw, end_of_month=True) or as_of
        if start is None:
            continue
        if end < start:
            start, end = end, start
        cursor = date(start.year, start.month, 1)
        last = date(end.year, end.month, 1)
        while cursor <= last:
            months.add((cursor.year, cursor.month))
            if cursor.month == 12:
                cursor = date(cursor.year + 1, 1, 1)
            else:
                cursor = date(cursor.year, cursor.month + 1, 1)
    years = len(months) / 12.0
    return round(years, 1)


def extract_required_years(
    job_title: str = "",
    job_description: str = "",
) -> ExperienceRequirement | None:
    """
    Parse an explicit years-of-experience requirement from title + description.

    Returns None when the posting does not specify years (qualify freely).
    When several requirements appear, the strictest minimum is used (seniority bar).
    """
    text = f"{job_title or ''}\n{job_description or ''}"
    if not text.strip():
        return None

    found: list[ExperienceRequirement] = []
    found.extend(_find_ranges(text))
    found.extend(_find_plus(text))
    found.extend(_find_at_least(text))
    found.extend(_find_plain_experience(text))

    filtered = [r for r in found if _looks_like_requirement(text, r)]
    filtered = _prefer_ranges(filtered)
    if not filtered:
        return None
    # Highest minimum is the seniority bar for the role.
    filtered.sort(key=lambda r: (r.min_years, r.max_years or 99))
    chosen = filtered[-1]
    logger.debug("YoE requirement from JD: %s (%r)", chosen.describes(), chosen.raw)
    return chosen


def meets_experience_requirement(
    candidate_years: float,
    requirement: ExperienceRequirement | None,
    *,
    max_slack: float = 0.5,
) -> bool:
    """True when JD has no YoE, or candidate years fall in the required band."""
    if requirement is None:
        return True
    if candidate_years + 1e-9 < requirement.min_years:
        return False
    if requirement.max_years is not None:
        if candidate_years > requirement.max_years + max_slack:
            return False
    return True


def experience_gate(
    profile: dict[str, Any],
    job_title: str = "",
    job_description: str = "",
    *,
    as_of: date | None = None,
) -> tuple[bool, str]:
    """
    Mandatory qualify check.

    Returns (qualified, reason). qualified=False means do not score/tailor/apply.
    """
    years = candidate_years_of_experience(profile, as_of=as_of)
    req = extract_required_years(job_title, job_description)
    if req is None:
        return True, (
            f"No years-of-experience stated in the posting; "
            f"qualified with {years:g} years from resume."
        )
    if meets_experience_requirement(years, req):
        return True, (
            f"Years of experience match: resume {years:g} years, "
            f"job asks {req.describes()}."
        )
    return False, (
        f"Years of experience mismatch: resume has {years:g} years, "
        f"job requires {req.describes()} ({req.raw!r})."
    )


def _is_present(raw: Any) -> bool:
    s = str(raw or "").strip()
    return bool(re.search(r"\b(present|current|now|ongoing)\b", s, re.I)) or not s


def _parse_date(raw: Any, *, end_of_month: bool) -> date | None:
    s = str(raw or "").strip()
    if not s:
        return None
    m = re.search(
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\s*[.,]?\s*((?:19|20)\d{2})",
        s,
        re.I,
    )
    if m:
        month = _MONTHS.get(re.sub(r"[^a-z]", "", m.group(1).lower()), 1)
        year = int(m.group(2))
        return _month_bound(year, month, end_of_month)
    mmy = re.search(r"\b(\d{1,2})/((?:19|20)\d{2})\b", s)
    if mmy:
        month = int(mmy.group(1))
        year = int(mmy.group(2))
        if 1 <= month <= 12:
            return _month_bound(year, month, end_of_month)
    y = re.search(r"\b((?:19|20)\d{2})\b", s)
    if y:
        year = int(y.group(1))
        month = 12 if end_of_month else 1
        return _month_bound(year, month, end_of_month)
    return None


def _month_bound(year: int, month: int, end_of_month: bool) -> date:
    day = calendar.monthrange(year, month)[1] if end_of_month else 1
    return date(year, month, day)


def _window(text: str, start: int, end: int, radius: int = 48) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)]


def _looks_like_requirement(text: str, req: ExperienceRequirement) -> bool:
    ctx = _window(text, req.start, req.end)
    if _EXCLUDE.search(ctx):
        return False
    if _ACADEMIC.search(ctx):
        return False
    return True


def _spans_overlap(a: ExperienceRequirement, b: ExperienceRequirement) -> bool:
    return a.start < b.end and b.start < a.end


def _prefer_ranges(found: list[ExperienceRequirement]) -> list[ExperienceRequirement]:
    """Drop '3 years of experience' nested inside '1-3 years of experience'."""
    ranges = [r for r in found if r.is_range()]
    if not ranges:
        return found
    kept: list[ExperienceRequirement] = list(ranges)
    for item in found:
        if item.is_range():
            continue
        if any(_spans_overlap(item, rng) for rng in ranges):
            continue
        kept.append(item)
    return kept


def _find_ranges(text: str) -> list[ExperienceRequirement]:
    out: list[ExperienceRequirement] = []
    pattern = re.compile(
        rf"(\d+(?:\.\d+)?)\s*(?:[-–—]|to)\s*(\d+(?:\.\d+)?)\s*\+?\s*{_YEARS_UNIT}"
        rf"(?:\s*(?:of\s+)?(?:professional\s+)?(?:experience|exp\b))?",
        re.I,
    )
    for m in pattern.finditer(text):
        lo, hi = float(m.group(1)), float(m.group(2))
        if hi < lo:
            lo, hi = hi, lo
        out.append(
            ExperienceRequirement(lo, hi, m.group(0).strip(), m.start(), m.end())
        )
    return out


def _find_plus(text: str) -> list[ExperienceRequirement]:
    out: list[ExperienceRequirement] = []
    pattern = re.compile(
        rf"(\d+(?:\.\d+)?)\s*\+\s*{_YEARS_UNIT}"
        rf"(?:\s*(?:of\s+)?(?:professional\s+)?(?:experience|exp\b))?",
        re.I,
    )
    for m in pattern.finditer(text):
        out.append(
            ExperienceRequirement(
                float(m.group(1)), None, m.group(0).strip(), m.start(), m.end()
            )
        )
    # "N years+"
    pattern2 = re.compile(
        rf"(\d+(?:\.\d+)?)\s*{_YEARS_UNIT}\s*\+"
        rf"(?:\s*(?:of\s+)?(?:professional\s+)?(?:experience|exp\b))?",
        re.I,
    )
    for m in pattern2.finditer(text):
        out.append(
            ExperienceRequirement(
                float(m.group(1)), None, m.group(0).strip(), m.start(), m.end()
            )
        )
    return out


def _find_at_least(text: str) -> list[ExperienceRequirement]:
    out: list[ExperienceRequirement] = []
    pattern = re.compile(
        rf"(?:at\s+least|minimum(?:\s+of)?|min(?:imum)?|must\s+have|"
        rf"or\s+more|or\s+greater)\s*"
        rf"(?:of\s+)?"
        rf"(\d+(?:\.\d+)?)\s*{_YEARS_UNIT}"
        rf"|"
        rf"(\d+(?:\.\d+)?)\s*{_YEARS_UNIT}\s*(?:or\s+more|or\s+greater|minimum)",
        re.I,
    )
    for m in pattern.finditer(text):
        num = m.group(1) or m.group(2)
        if not num:
            continue
        out.append(
            ExperienceRequirement(
                float(num), None, m.group(0).strip(), m.start(), m.end()
            )
        )
    return out


def _find_plain_experience(text: str) -> list[ExperienceRequirement]:
    """'N years of experience' / 'N years' experience' — not company history."""
    out: list[ExperienceRequirement] = []
    pattern = re.compile(
        rf"(\d+(?:\.\d+)?)\s*{_YEARS_UNIT}\s*"
        rf"(?:['’]?\s*)?(?:of\s+)?(?:professional\s+)?(?:relevant\s+)?"
        rf"(?:experience|exp\b)",
        re.I,
    )
    for m in pattern.finditer(text):
        # Plain "N years of experience" means at least N (JD convention).
        out.append(
            ExperienceRequirement(
                float(m.group(1)), None, m.group(0).strip(), m.start(), m.end()
            )
        )
    return out


# Used by tests / debugging without importing datetime in callers.
def _today() -> date:
    return datetime.now().date()
