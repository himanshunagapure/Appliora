"""
Location preference as a *soft* scoring signal.

Unlike years of experience, location never skips a job. Nagpur / remote / WFH
are preferred; onsite-elsewhere is a modest penalty; unspecified is neutral.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from utils.logger import get_logger

logger = get_logger("jobapplypro.location")

LocationFit = Literal["preferred", "elsewhere", "unspecified"]

DEFAULT_PREFERENCES = ("Nagpur", "Remote", "WFH")

# Modest so location ranks jobs but cannot knock a strong skill match out
# the way a YoE mismatch does.
PREFERRED_DELTA = 0.4
ELSEWHERE_DELTA = -0.4

_REMOTE = re.compile(
    r"\b("
    r"remote(?:[ -]only|[ -]first|[ -]friendly)?"
    r"|work[ -]?from[ -]?home"
    r"|wfh"
    r"|wfa"
    r"|telecommut(?:e|ing)"
    r"|fully[ -]remote"
    r"|distributed team"
    r")\b",
    re.I,
)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_NAGPUR = re.compile(r"\bnagpur\b", re.I)
_ONSITE = re.compile(
    r"\b("
    r"on[ -]?site"
    r"|in[ -]?office"
    r"|in person"
    r"|must relocate"
    r"|relocation required"
    r"|office (?:in|at|based)"
    r"|based in"
    r")\b",
    re.I,
)


def location_preferences(profile: dict[str, Any] | None) -> list[str]:
    """Profile list, or contact city plus Remote/WFH."""
    if profile:
        raw = profile.get("location_preferences")
        if isinstance(raw, list) and raw:
            return [str(p).strip() for p in raw if str(p).strip()]
        contact = profile.get("contact") or {}
        city = str(contact.get("location") or "").split(",")[0].strip()
        prefs = [city] if city else []
        for extra in DEFAULT_PREFERENCES:
            if extra.lower() not in {p.lower() for p in prefs}:
                prefs.append(extra)
        return prefs
    return list(DEFAULT_PREFERENCES)


def classify_job_location(job_title: str = "", job_description: str = "") -> LocationFit:
    text = f"{job_title or ''}\n{job_description or ''}"
    if not text.strip():
        return "unspecified"
    if _NAGPUR.search(text) or _REMOTE.search(text) or _HYBRID.search(text):
        return "preferred"
    if _ONSITE.search(text):
        return "elsewhere"
    return "unspecified"


def location_score_adjustment(
    profile: dict[str, Any] | None,
    job_title: str = "",
    job_description: str = "",
) -> tuple[float, str]:
    """
    Returns (delta, reason). Delta is added to the 1–10 fit score.
    Never used as a skip/qualify gate.
    """
    prefs = location_preferences(profile)
    pref_label = " / ".join(prefs)
    fit = classify_job_location(job_title, job_description)
    if fit == "preferred":
        return PREFERRED_DELTA, (
            f"Location preference match ({pref_label}): "
            f"+{PREFERRED_DELTA:g} (soft, not a knockout)."
        )
    if fit == "elsewhere":
        return ELSEWHERE_DELTA, (
            f"Location is onsite elsewhere vs preference {pref_label}: "
            f"{ELSEWHERE_DELTA:g} (soft penalty; job is still scored)."
        )
    return 0.0, (
        f"Location unspecified in posting; preference {pref_label} not applied."
    )
