"""Shared types for Phase 7 application automation (FR6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FieldMapping:
    """One form control mapped to a logical profile/application key."""

    name: str  # email, phone, resume, cover_letter, ...
    selector: str
    field_type: str = "text"  # text | textarea | select | checkbox | radio | file | unknown
    required: bool = False
    confidence: float = 0.5


@dataclass
class FormPlan:
    """Detection result used by site adapters to fill an application form."""

    ats: str  # linkedin_easy_apply | workday | generic | unknown
    fields: list[FieldMapping] = field(default_factory=list)
    apply_button: str | None = None
    submit_button: str | None = None
    multi_page: bool = False
    confidence: float = 0.0
    detection_tier: int = 0  # 1=url/json-ld, 2=css, 3=llm
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ats": self.ats,
            "fields": [f.__dict__ for f in self.fields],
            "apply_button": self.apply_button,
            "submit_button": self.submit_button,
            "multi_page": self.multi_page,
            "confidence": self.confidence,
            "detection_tier": self.detection_tier,
            "reason": self.reason,
        }


@dataclass
class FillResult:
    """Outcome of a single dry-run fill or submit attempt."""

    status: str  # filled | submitted | manual_review | failed | expired | skipped
    screenshot_path: str | None = None
    error: str | None = None
    fields_filled: list[str] = field(default_factory=list)
    fields_skipped: list[str] = field(default_factory=list)
    adapter: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "screenshot_path": self.screenshot_path,
            "error": self.error,
            "fields_filled": self.fields_filled,
            "fields_skipped": self.fields_skipped,
            "adapter": self.adapter,
        }
