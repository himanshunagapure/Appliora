"""
Prepare resumes for ATS / Easy Apply uploads.

LinkedIn Easy Apply accepts DOC/DOCX/PDF only (<2MB). Tailored resumes in this
project are markdown, so we convert to DOCX on demand.
"""

from __future__ import annotations

import re
from pathlib import Path

from utils.logger import get_logger

logger = get_logger("jobapplypro.resume_upload")

UPLOADABLE_SUFFIXES = {".pdf", ".doc", ".docx"}


def ensure_uploadable_resume(path: str | Path, *, output_dir: Path | None = None) -> Path:
    """
    Return a path LinkedIn/ATS file inputs will accept.

    - PDF/DOC/DOCX are returned as-is.
    - Markdown/text are converted to a sibling .docx (cached if newer than source).
    """
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"Resume not found: {src}")

    suffix = src.suffix.lower()
    if suffix in UPLOADABLE_SUFFIXES:
        return src

    if suffix not in {".md", ".markdown", ".txt", ""}:
        raise ValueError(
            f"Unsupported resume format {suffix!r}; need pdf/doc/docx or md/txt"
        )

    out_dir = output_dir or src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{src.stem}.docx"

    if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest

    markdown_to_docx(src.read_text(encoding="utf-8"), dest)
    logger.info("Converted resume %s → %s", src.name, dest)
    return dest


def markdown_to_docx(markdown: str, dest: Path) -> Path:
    """Best-effort markdown → DOCX for upload (not a full renderer)."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Horizontal rules / markdown separators
        if re.fullmatch(r"-{3,}|_{3,}|\*{3,}", stripped):
            i += 1
            continue

        # Headings
        heading_match = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading_match:
            level = min(len(heading_match.group(1)), 2)
            doc.add_heading(_strip_md_inline(heading_match.group(2)), level=level)
            i += 1
            continue

        # Section labels like "Skills" followed by --- on next line already handled
        if i + 1 < len(lines) and re.fullmatch(r"-{3,}", lines[i + 1].strip()):
            doc.add_heading(_strip_md_inline(stripped), level=1)
            i += 2
            continue

        # Bullets
        bullet_match = re.match(r"^(\*|-|\u2022)\s+(.*)$", stripped)
        if bullet_match:
            doc.add_paragraph(_strip_md_inline(bullet_match.group(2)), style="List Bullet")
            i += 1
            continue

        # Bold-led experience lines: **Company** | Role | Dates
        doc.add_paragraph(_strip_md_inline(stripped))
        i += 1

    dest.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(dest))
    return dest


def _strip_md_inline(text: str) -> str:
    """Remove light markdown emphasis / links for plain DOCX text."""
    s = text
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*]+)\*", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    return s.strip()
