"""
Phase 11 — proxy list loading and rotation for JobSpy discovery.

JobSpy accepts `proxies=["user:pass@host:port", "host:port"]` and rotates
internally. We also rotate the list order per scrape so each search starts
on a different endpoint.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from utils.logger import get_logger

logger = get_logger("jobapplypro.proxies")

_rotate_counter = itertools.count()


def parse_proxy_urls(raw: str | None) -> list[str]:
    """Split a comma/newline/semicolon-separated env string into JobSpy proxies."""
    if not raw:
        return []
    parts: list[str] = []
    for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
        item = chunk.strip()
        if item and not item.startswith("#"):
            parts.append(_normalize_proxy(item))
    return [p for p in parts if p]


def load_proxy_file(path: str | Path | None) -> list[str]:
    if not path:
        return []
    p = Path(path).expanduser()
    if not p.is_absolute():
        from config.settings import PROJECT_ROOT

        p = PROJECT_ROOT / p
    if not p.exists():
        logger.warning("Proxy file not found: %s", p)
        return []
    out: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        out.append(_normalize_proxy(item))
    return [x for x in out if x]


def load_proxies(settings=None) -> list[str]:
    """Union of PROXY_URLS and PROXY_FILE, de-duplicated, order preserved."""
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    urls = parse_proxy_urls(getattr(settings, "proxy_urls", "") or "")
    file_urls = load_proxy_file(getattr(settings, "proxy_file", None))
    seen: set[str] = set()
    merged: list[str] = []
    for item in urls + file_urls:
        if item not in seen:
            seen.add(item)
            merged.append(item)
    return merged


def rotate_proxies(proxies: Iterable[str], offset: int | None = None) -> list[str]:
    """Return a copy of proxies rotated so scrape N starts at a different host."""
    items = [p for p in proxies if p]
    if not items:
        return []
    if offset is None:
        offset = next(_rotate_counter)
    n = len(items)
    start = offset % n
    return items[start:] + items[:start]


def redact_jobspy_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Log-safe copy — never dump proxy credentials."""
    out = dict(kwargs)
    proxies = out.get("proxies")
    if proxies:
        out["proxies"] = f"<{len(list(proxies))} proxies>"
    return out


def _normalize_proxy(value: str) -> str:
    """
    JobSpy format_proxy() keeps http(s)/socks5 URLs and prefixes host:port
    with http://. Do not strip socks5:// — that would be turned into HTTP.
    """
    text = value.strip()
    lower = text.lower()
    if lower.startswith(("socks5://", "socks4://", "http://", "https://")):
        return text
    if "://" in text:
        parsed = urlparse(text)
        host = parsed.hostname or ""
        port = parsed.port
        if not host:
            return ""
        auth = ""
        if parsed.username:
            password = parsed.password or ""
            auth = f"{parsed.username}:{password}@"
        hostport = f"{host}:{port}" if port else host
        return f"{auth}{hostport}"
    return text
