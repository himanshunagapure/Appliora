"""Site adapter package — LinkedIn Easy Apply, Workday, and generic HTML forms."""

from __future__ import annotations

from typing import Any, Protocol

from automation.types import FillResult, FormPlan


class SiteAdapter(Protocol):
    name: str

    def matches(self, url: str, html: str = "") -> bool: ...

    def fill(
        self,
        page,
        plan: FormPlan,
        profile: dict[str, Any],
        materials: dict[str, Any],
        *,
        dry_run: bool = True,
    ) -> FillResult: ...
