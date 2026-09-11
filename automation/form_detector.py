"""
Form detection cascade for Phase 7 (FR6):

  Tier 1 — URL / ATS fingerprint (+ optional JSON-LD JobPosting hints)
  Tier 2 — CSS / label / name / aria heuristics for inputs
  Tier 3 — LLM mapping of remaining unlabeled controls (optional)

Low confidence or CAPTCHA → caller routes job to manual_review.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from automation.browser import html_looks_like_captcha
from automation.types import FieldMapping, FormPlan
from config.settings import get_settings
from llm.client import LLMClient, LLMError
from utils.logger import get_logger

logger = get_logger("jobapplypro.form_detector")

# Logical keys we try to map onto form controls.
LOGICAL_KEYS = (
    "full_name",
    "first_name",
    "last_name",
    "email",
    "phone",
    "city",
    "location",
    "linkedin_url",
    "resume",
    "cover_letter",
    "work_authorized",
    "sponsorship",
    "salary",
    "start_date",
)

# name/id/placeholder/aria substrings → logical key
_FIELD_HINTS: list[tuple[str, str]] = [
    ("email", "email"),
    ("e-mail", "email"),
    ("phone", "phone"),
    ("mobile", "phone"),
    ("tel", "phone"),
    ("first.?name", "first_name"),
    ("fname", "first_name"),
    ("last.?name", "last_name"),
    ("lname", "last_name"),
    ("full.?name", "full_name"),
    ("applicant.?name", "full_name"),
    ("your.?name", "full_name"),
    ("city", "city"),
    ("location", "location"),
    ("linkedin", "linkedin_url"),
    ("resume", "resume"),
    ("cv", "resume"),
    ("cover.?letter", "cover_letter"),
    ("coverletter", "cover_letter"),
    ("work.?auth", "work_authorized"),
    ("authorized.?to.?work", "work_authorized"),
    ("sponsor", "sponsorship"),
    ("salary", "salary"),
    ("compensation", "salary"),
    ("start.?date", "start_date"),
    ("earliest", "start_date"),
]

APPLY_BUTTON_SELECTORS = [
    "button:has-text('Easy Apply')",
    "button:has-text('Apply')",
    "a:has-text('Easy Apply')",
    "a:has-text('Apply Now')",
    "a:has-text('Apply')",
    "[data-testid*='apply' i]",
    "[aria-label*='Apply' i]",
    "a[href*='apply' i]",
    "button[class*='apply' i]",
]

SUBMIT_BUTTON_SELECTORS = [
    "button:has-text('Submit application')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
    "input[type='submit']",
    "[data-testid*='submit' i]",
    "[aria-label*='Submit' i]",
]


def load_ats_config(path: str | Path | None = None) -> dict[str, Any]:
    settings = get_settings()
    cfg_path = Path(path or settings.ats_config_path)
    if not cfg_path.exists():
        return {
            "manual_ats": [],
            "tier2_ats": [],
            "blocked_sso": [],
            "captcha_signals": [],
        }
    with open(cfg_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def host_matches_domain(hostname: str, domain: str) -> bool:
    """True when hostname is exactly domain or a subdomain of it (not a substring)."""
    host = (hostname or "").lower().rstrip(".")
    token = str(domain or "").lower().strip().lstrip(".")
    if not host or not token:
        return False
    return host == token or host.endswith("." + token)


def detect_ats(url: str, html: str = "") -> str:
    """Return ATS family from URL (+ light HTML fingerprints)."""
    host = (urlparse(url).hostname or "").lower()
    path = (urlparse(url).path or "").lower()
    query = (urlparse(url).query or "").lower()
    lower_html = (html or "").lower()

    if host_matches_domain(host, "linkedin.com"):
        return "linkedin_easy_apply"
    if (
        host_matches_domain(host, "myworkdayjobs.com")
        or host_matches_domain(host, "workday.com")
        or host_matches_domain(host, "myworkdaysite.com")
    ):
        return "workday"
    if (
        "gh_jid=" in query
        or "gh_jid=" in (url or "").lower()
        or host_matches_domain(host, "greenhouse.io")
        or "grnhse_app" in lower_html
        or "job-boards.greenhouse" in lower_html
    ):
        return "greenhouse"
    if host_matches_domain(host, "lever.co") or "jobs.lever.co" in lower_html or "postings.lever.co" in lower_html:
        return "lever"
    if host_matches_domain(host, "ashbyhq.com") or "powered by ashby" in lower_html:
        return "ashby"
    if (
        host_matches_domain(host, "workable.com")
        or "apply.workable.com" in lower_html
        or "powered by workable" in lower_html
    ):
        return "workable"
    # Workday sometimes embeds in iframes on company sites
    if "workday" in lower_html and "myworkday" in lower_html:
        return "workday"
    if path.endswith("/apply") or "/apply/" in path:
        return "generic"
    return "generic"


def is_manual_ats(url: str, ats_config: dict[str, Any] | None = None) -> bool:
    """
    True when the host is listed under tier2_ats or legacy manual_ats.

    Historically these domains went straight to manual_review. Under ADR-002
    they route to the Tier 2 browser-use agent instead; the name is kept for
    compatibility with existing callers/tests.
    """
    cfg = ats_config or load_ats_config()
    host = (urlparse(url).hostname or "").lower()
    domains = list(cfg.get("tier2_ats") or []) + list(cfg.get("manual_ats") or [])
    seen: set[str] = set()
    for domain in domains:
        token = str(domain).lower().strip()
        if not token or token in seen:
            continue
        seen.add(token)
        if host_matches_domain(host, token):
            return True
    return False


def is_blocked_sso(url: str, ats_config: dict[str, Any] | None = None) -> bool:
    cfg = ats_config or load_ats_config()
    host = (urlparse(url).hostname or "").lower()
    for domain in cfg.get("blocked_sso") or []:
        if host_matches_domain(host, str(domain)):
            return True
    return False


def detect_captcha(html: str, ats_config: dict[str, Any] | None = None) -> bool:
    cfg = ats_config or load_ats_config()
    signals = cfg.get("captcha_signals") or None
    return html_looks_like_captcha(html, signals=signals)


def extract_json_ld(html: str) -> list[dict[str, Any]]:
    """Parse application/ld+json blocks from HTML."""
    blocks: list[dict[str, Any]] = []
    pattern = re.compile(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html or ""):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            blocks.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            blocks.append(data)
    return blocks


def _hint_to_logical(text: str) -> str | None:
    blob = (text or "").strip().lower()
    if not blob:
        return None
    for pattern, key in _FIELD_HINTS:
        if re.search(pattern, blob, re.IGNORECASE):
            return key
    return None


def _infer_field_type(tag: str, input_type: str, accept: str = "") -> str:
    tag = (tag or "").lower()
    input_type = (input_type or "text").lower()
    if tag == "textarea":
        return "textarea"
    if tag == "select":
        return "select"
    if input_type == "file" or "pdf" in (accept or "").lower():
        return "file"
    if input_type in {"checkbox"}:
        return "checkbox"
    if input_type in {"radio"}:
        return "radio"
    if input_type == "email":
        return "text"
    return "text"


def detect_fields_from_html(html: str) -> list[FieldMapping]:
    """
    Tier-2 CSS/heuristic extraction that works without a live Playwright page.
    Parses common input/textarea/select attributes from HTML.
    """
    fields: list[FieldMapping] = []
    seen_names: set[str] = set()

    # Rough but effective for fixtures + many real pages.
    control_re = re.compile(
        r"<(input|textarea|select)\b([^>]*)>",
        re.IGNORECASE,
    )
    attr_re = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:]*)\s*=\s*("([^"]*)"|\'([^\']*)\')')

    for match in control_re.finditer(html or ""):
        tag = match.group(1).lower()
        attrs_raw = match.group(2)
        attrs: dict[str, str] = {}
        for am in attr_re.finditer(attrs_raw):
            key = am.group(1).lower()
            val = am.group(3) if am.group(3) is not None else am.group(4)
            attrs[key] = val or ""

        input_type = attrs.get("type", "text").lower()
        if input_type in {"hidden", "submit", "button", "image", "reset"}:
            continue

        hint_blob = " ".join(
            [
                attrs.get("name", ""),
                attrs.get("id", ""),
                attrs.get("placeholder", ""),
                attrs.get("aria-label", ""),
                attrs.get("autocomplete", ""),
            ]
        )
        logical = _hint_to_logical(hint_blob)
        if not logical:
            continue
        if logical in seen_names:
            continue
        seen_names.add(logical)

        # Prefer a stable CSS selector.
        if attrs.get("id"):
            selector = f"#{attrs['id']}"
        elif attrs.get("name"):
            selector = f"[name='{attrs['name']}']"
        elif attrs.get("aria-label"):
            selector = f"[aria-label='{attrs['aria-label']}']"
        else:
            continue

        field_type = _infer_field_type(tag, input_type, attrs.get("accept", ""))
        # File inputs named resume/cv get resume; cover letter textareas mapped already.
        if field_type == "file" and logical not in {"resume", "cover_letter"}:
            logical = "resume"

        required = "required" in attrs_raw.lower()
        confidence = 0.75 if attrs.get("name") or attrs.get("id") else 0.55
        fields.append(
            FieldMapping(
                name=logical,
                selector=selector,
                field_type=field_type,
                required=required,
                confidence=confidence,
            )
        )

    return fields


def detect_fields_css(page) -> list[FieldMapping]:
    """
    Tier-2 extraction using a live Playwright page when available.
    Falls back to HTML parsing of page.content().
    """
    try:
        html = page.content()
    except Exception:
        return []
    return detect_fields_from_html(html)


def detect_fields_llm(
    html_snippet: str,
    profile_keys: list[str],
    llm_client: LLMClient,
) -> list[FieldMapping]:
    """Tier-3: ask the LLM to map remaining controls to logical keys."""
    snippet = (html_snippet or "")[:8000]
    keys = ", ".join(profile_keys or list(LOGICAL_KEYS))
    prompt = f"""You map HTML form controls to logical job-application fields.

Allowed logical names (use ONLY these): {keys}

Given this HTML snippet of an application form, return JSON:
{{
  "fields": [
    {{"name": "<logical>", "selector": "<css selector>", "field_type": "text|textarea|select|file|checkbox|radio", "required": false, "confidence": 0.0}}
  ]
}}

Rules:
- Prefer stable CSS selectors (id, name, aria-label).
- confidence is 0-1.
- Skip submit/hidden/buttons.
- If unsure, omit the field rather than guessing wildly.

HTML:
{snippet}
"""
    try:
        result = llm_client.generate_json(prompt)
    except LLMError as exc:
        logger.warning("LLM form detection failed: %s", exc)
        return []

    fields: list[FieldMapping] = []
    for item in result.get("fields") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        selector = str(item.get("selector") or "").strip()
        if not name or not selector or name not in (profile_keys or LOGICAL_KEYS):
            continue
        fields.append(
            FieldMapping(
                name=name,
                selector=selector,
                field_type=str(item.get("field_type") or "text"),
                required=bool(item.get("required")),
                confidence=float(item.get("confidence") or 0.5),
            )
        )
    return fields


def _first_matching_selector(html: str, selectors: list[str]) -> str | None:
    """
    Lightweight check: for :has-text selectors, look for the phrase as visible
    element copy (or value=). Word boundaries so 'Apply' does not match
    'application', and attribute-only hits like type="submit" are ignored.
    """
    raw = html or ""
    lower = raw.lower()
    for sel in selectors:
        m = re.search(r":has-text\('([^']+)'\)", sel)
        if m:
            needle = m.group(1)
            visible = (
                r">\s*[^<]*\b" + re.escape(needle) + r"\b[^<]*<"
                r"|value\s*=\s*[\"'][^\"']*\b" + re.escape(needle) + r"\b[^\"']*[\"']"
                r"|aria-label\s*=\s*[\"'][^\"']*\b" + re.escape(needle) + r"\b[^\"']*[\"']"
            )
            if re.search(visible, raw, re.IGNORECASE):
                return sel
            continue
        if "easy apply" in sel.lower() and "easy apply" in lower:
            return sel
        if "submit application" in sel.lower() and re.search(
            r">\s*[^<]*submit application[^<]*<", raw, re.IGNORECASE
        ):
            return sel
        if "data-testid" in sel and "apply" in lower:
            return sel
        if sel.startswith("a[href*='apply") and "href" in lower and "apply" in lower:
            return sel
        if sel == "input[type='submit']" and (
            'type="submit"' in lower or "type='submit'" in lower
        ):
            return sel
    return None


def build_form_plan(
    url: str,
    html: str,
    *,
    page=None,
    llm_client: LLMClient | None = None,
    min_confidence: float | None = None,
    use_llm: bool = False,
) -> FormPlan:
    """
    Run the detection cascade and return a FormPlan.
    Prefer passing `html` (from fixtures or page.content()); `page` enables live CSS.
    """
    settings = get_settings()
    min_confidence = (
        settings.form_min_confidence if min_confidence is None else min_confidence
    )
    ats_config = load_ats_config()

    if detect_captcha(html, ats_config):
        return FormPlan(
            ats="unknown",
            confidence=0.0,
            detection_tier=0,
            reason="captcha",
        )

    if is_manual_ats(url, ats_config):
        # Signal Tier 2 routing (ADR-002); core.apply sends these to the agent
        # instead of immediate manual_review.
        return FormPlan(
            ats="unknown",
            confidence=0.0,
            detection_tier=1,
            reason="tier2_ats",
        )

    if is_blocked_sso(url, ats_config):
        return FormPlan(
            ats="unknown",
            confidence=0.0,
            detection_tier=1,
            reason="sso_required",
        )

    ats = detect_ats(url, html)
    json_ld = extract_json_ld(html)
    tier = 1 if json_ld else 0

    fields: list[FieldMapping] = []
    if page is not None:
        fields = detect_fields_css(page)
    if not fields:
        fields = detect_fields_from_html(html)
    if fields:
        tier = max(tier, 2)

    if use_llm and llm_client is not None and len(fields) < 2:
        llm_fields = detect_fields_llm(html, list(LOGICAL_KEYS), llm_client)
        existing = {f.name for f in fields}
        for f in llm_fields:
            if f.name not in existing:
                fields.append(f)
        if llm_fields:
            tier = 3

    apply_btn = _first_matching_selector(html, APPLY_BUTTON_SELECTORS)
    submit_btn = _first_matching_selector(html, SUBMIT_BUTTON_SELECTORS)

    # Confidence: mapped core contact fields matter most.
    core = {"email", "full_name", "first_name", "phone", "resume"}
    mapped_core = {f.name for f in fields} & core
    if not fields:
        confidence = 0.15 if ats in {"linkedin_easy_apply", "workday"} else 0.05
    else:
        confidence = min(0.95, 0.35 + 0.15 * len(fields) + 0.1 * len(mapped_core))

    # Known ATS families get a floor even with few mapped fields — adapters
    # drive most of the interaction for LinkedIn/Workday/Greenhouse/Ashby.
    if ats in {"linkedin_easy_apply", "workday", "greenhouse", "ashby"}:
        confidence = max(confidence, 0.6)

    reason = None
    if confidence < min_confidence and ats == "generic":
        reason = "unrecognized_form"
        ats_out = "unknown"
    else:
        ats_out = ats

    multi_page = ats_out in {"linkedin_easy_apply", "workday", "greenhouse", "ashby"}

    return FormPlan(
        ats=ats_out,
        fields=fields,
        apply_button=apply_btn,
        submit_button=submit_btn,
        multi_page=multi_page,
        confidence=confidence,
        detection_tier=tier,
        reason=reason,
    )


def profile_value_map(profile: dict[str, Any]) -> dict[str, str]:
    """Flatten profile (+ optional apply_answers) into fillable string values."""
    contact = profile.get("contact") or {}
    answers = profile.get("apply_answers") or {}
    name = str(contact.get("name") or "").strip()
    parts = name.split(None, 1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""

    values: dict[str, str] = {
        "full_name": name,
        "first_name": first,
        "last_name": last,
        "email": str(contact.get("email") or ""),
        "phone": str(contact.get("phone") or ""),
        "location": str(contact.get("location") or ""),
        "city": "",
        "linkedin_url": str(contact.get("linkedin_url") or ""),
        "work_authorized": str(
            answers.get("work_authorized")
            or answers.get("work_authorization")
            or ""
        ),
        "sponsorship": str(answers.get("sponsorship") or ""),
        "salary": str(
            answers.get("salary") or answers.get("salary_expectation") or ""
        ),
        "start_date": str(
            answers.get("start_date") or answers.get("earliest_start") or ""
        ),
        # Common screening defaults — only filled when present in apply_answers.
        "over_18": str(answers.get("over_18") or ""),
        "background_check": str(answers.get("background_check") or ""),
        "felony": str(answers.get("felony") or answers.get("criminal_record") or ""),
        "how_heard": str(answers.get("how_heard") or ""),
        "how_heard_category": str(
            answers.get("how_heard_category") or answers.get("how_heard_parent") or ""
        ),
        "previously_worked_here": str(
            answers.get("previously_worked_here")
            or answers.get("previous_worker")
            or ""
        ),
        "address_line1": str(
            answers.get("address_line1") or answers.get("address") or ""
        ),
        "postal_code": str(answers.get("postal_code") or answers.get("zip") or ""),
        "eeo_gender": str(answers.get("eeo_gender") or answers.get("gender") or ""),
        "eeo_race": str(answers.get("eeo_race") or answers.get("race") or ""),
        "eeo_veteran": str(answers.get("eeo_veteran") or answers.get("veteran") or ""),
        "eeo_disability": str(
            answers.get("eeo_disability") or answers.get("disability") or ""
        ),
    }
    # Prefer-not-to-say defaults for EEO when explicitly requested.
    if answers.get("eeo_decline"):
        for key in ("eeo_gender", "eeo_race", "eeo_veteran", "eeo_disability"):
            values[key] = values[key] or "Prefer not to say"

    loc = values["location"]
    if loc and "," in loc:
        values["city"] = loc.split(",", 1)[0].strip()
    elif loc:
        values["city"] = loc
    if not values.get("postal_code") and loc:
        m = re.search(r"\b(\d{5,6})\b", loc)
        if m:
            values["postal_code"] = m.group(1)
    return {k: v for k, v in values.items() if v}
