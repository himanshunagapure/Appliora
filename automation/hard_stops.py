"""
Hard-stop detection for Phase 7 apply (FR6).

Mirrors ApplyPilot's behavioral "NEVER DO THESE" / RESULT taxonomy without
copying AGPL code or CapSolver. Human-recoverable blocks → manual_review;
policy skips → skipped; closed jobs → expired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from automation.form_detector import is_blocked_sso, is_manual_ats, load_ats_config
from utils.logger import get_logger

logger = get_logger("jobapplypro.hard_stops")

# Domains in tier2_ats / manual_ats are *not* hard-stops anymore — they route to
# the Tier 2 browser-use agent (ADR-002). Use skip_tier2_ats=True (default for
# Tier 2 preflight) or omit the check via the flag below.


@dataclass(frozen=True)
class HardStop:
    """Structured early-exit reason before/during form fill."""

    code: str
    # How core.apply should classify this: manual_review | failed | skipped | expired
    outcome: str
    detail: str = ""

    @property
    def error(self) -> str:
        return f"{self.code}: {self.detail}" if self.detail else self.code


# Visible page copy that means the posting is closed.
_EXPIRED_PATTERNS = (
    r"no longer accepting\s+applications",
    r"job\s+(has\s+)?expired",
    r"this\s+position\s+has\s+been\s+filled",
    r"position\s+is\s+no\s+longer\s+available",
    r"this\s+job\s+is\s+closed",
    r"applications?\s+are\s+closed",
    r"listing\s+has\s+been\s+removed",
    r"job\s+posting\s+is\s+no\s+longer\s+available",
)

_ALREADY_APPLIED_PATTERNS = (
    r"already\s+applied",
    r"you\s+have\s+already\s+submitted",
    r"application\s+was\s+submitted",
    r"you\s+applied\s+for\s+this\s+job",
)

# Workday Sign In HTML often includes "already applied" in bundled JS/copy.
_AUTH_WALL_PATTERNS = (
    r"forgot\s+your\s+password",
    r"sign\s+in\s+with\s+email",
    r"sign\s+in\s+with\s+google",
    r"don't\s+have\s+an\s+account\s+yet",
)

_UNSAFE_VERIFICATION_PATTERNS = (
    r"take\s+a\s+selfie",
    r"upload\s+(a\s+)?(photo\s+)?id\b",
    r"government[- ]issued\s+id",
    r"biometric",
    r"video\s+verification",
    r"record\s+a\s+video",
    r"webcam\s+required",
    r"identity\s+verification",
    r"scan\s+your\s+(face|id)",
)

_PAYMENT_PATTERNS = (
    r"credit\s+card\s*(number|#)",  # tightened — avoids "credit for waiving"
    r"debit\s+card",
    r"bank\s+account\s+number",
    r"routing\s+number",
    r"\bssn\b",
    r"social\s+security\s+(number|no\.?)",
    # "sin number" removed — too broad (fires on Singapore, business, Basin)
    r"(?:enter|provide|your)\s+payment\s+method",  # only form-prompt context
)

_MARKETPLACE_HOSTS = (
    "mercor.com",
    "toptal.com",
    "upwork.com",
    "fiverr.com",
    "turing.com",
    "freelancer.com",
)

_MARKETPLACE_PATTERNS = (
    r"set\s+your\s+(hourly\s+)?rate",
    r"contractor\s+marketplace",
    r"join\s+our\s+talent\s+network",
    r"freelance\s+marketplace",
)


def is_aggregator_auth_wall(url: str) -> bool:
    """Indeed/Naukri login or bot-detection pages — not the application form."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if host.endswith("indeed.com"):
        if "/auth" in path or "bot-detection" in query or "from=login" in query:
            return True
    if host.endswith("naukri.com") and any(
        token in path for token in ("/nlogin", "/login", "/mnjlogin")
    ):
        return True
    return False


def _match_any(text: str, patterns: tuple[str, ...]) -> str | None:
    lower = text or ""
    for pat in patterns:
        if re.search(pat, lower, re.IGNORECASE):
            return pat
    return None


def is_url_blocked(url: str, ats_config: dict[str, Any] | None = None) -> bool:
    """True when URL matches config blocked.sites / blocked.url_patterns."""
    cfg = ats_config or load_ats_config()
    blocked = cfg.get("blocked") or {}
    url_l = (url or "").lower()
    host = (urlparse(url).hostname or "").lower()

    for site in blocked.get("sites") or []:
        token = str(site).lower().strip()
        if token and (token in host or token in url_l):
            return True

    for pattern in blocked.get("url_patterns") or []:
        # Support ApplyPilot-style %wildcard% as substring, else literal substring.
        raw = str(pattern).lower().strip()
        needle = raw.strip("%")
        if needle and needle in url_l:
            return True
    return False


def detect_hard_stop(
    url: str,
    html: str = "",
    *,
    ats_config: dict[str, Any] | None = None,
    skip_tier2_ats: bool = True,
) -> HardStop | None:
    """
    Inspect URL + HTML for conditions that must stop automation immediately.
    Returns None when it is safe to continue filling.

    skip_tier2_ats (default True): domains listed under tier2_ats / manual_ats
    are handled by the Tier 2 agent, not treated as an immediate manual_review
    hard-stop. Pass skip_tier2_ats=False only for legacy callers that still
    want the old "force manual_review" behavior.
    """
    cfg = ats_config or load_ats_config()

    if is_url_blocked(url, cfg):
        return HardStop("site_blocked", "skipped", "URL matched blocked sites/patterns")

    if not skip_tier2_ats and is_manual_ats(url, cfg):
        return HardStop(
            "manual_ats",
            "manual_review",
            "ATS domain requires human apply (Tier 2 disabled path)",
        )

    if is_blocked_sso(url, cfg):
        return HardStop("sso_required", "manual_review", f"SSO wall at {urlparse(url).hostname}")

    host = (urlparse(url).hostname or "").lower()
    if any(m in host for m in _MARKETPLACE_HOSTS):
        return HardStop(
            "not_a_job_application",
            "skipped",
            f"contractor marketplace host: {host}",
        )

    if is_aggregator_auth_wall(url):
        return HardStop(
            "sso_required",
            "manual_review",
            "job-board login / bot-detection wall — sign in via BROWSER_USER_DATA_DIR",
        )

    text = html or ""
    if not text:
        return None

    if _match_any(text, _EXPIRED_PATTERNS):
        return HardStop("expired", "expired", "job closed / not accepting applications")

    if _match_any(text, _ALREADY_APPLIED_PATTERNS) and not _match_any(
        text, _AUTH_WALL_PATTERNS
    ):
        return HardStop("already_applied", "skipped", "candidate already applied")

    if _match_any(text, _UNSAFE_VERIFICATION_PATTERNS):
        return HardStop(
            "unsafe_verification",
            "manual_review",
            "ID / selfie / biometric verification required",
        )

    matched_payment = _match_any(text, _PAYMENT_PATTERNS)
    if matched_payment:
        logger.debug("unsafe_payment triggered by pattern: %r", matched_payment)
        return HardStop(
            "unsafe_payment",
            "manual_review",
            "page asks for payment / SSN / bank details — never enter these",
        )

    if _match_any(text, _MARKETPLACE_PATTERNS):
        return HardStop(
            "not_a_job_application",
            "skipped",
            "page looks like a talent marketplace / rate-setting flow",
        )

    return None


def hard_stop_to_fill_status(stop: HardStop) -> str:
    """Map HardStop.outcome onto FillResult.status values."""
    if stop.outcome == "expired":
        return "expired"
    if stop.outcome == "skipped":
        return "skipped"
    if stop.outcome == "manual_review":
        return "manual_review"
    return "failed"
