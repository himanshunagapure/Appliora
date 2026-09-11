"""Tests for resume upload helpers (markdown → DOCX for Easy Apply)."""

from pathlib import Path

from automation.resume_upload import ensure_uploadable_resume, markdown_to_docx


def test_markdown_to_docx_creates_file(tmp_path: Path):
    md = "# Jane Doe\n\nSkills\n---\n* Python\n* FastAPI\n"
    dest = tmp_path / "resume.docx"
    out = markdown_to_docx(md, dest)
    assert out.exists()
    assert out.suffix == ".docx"
    assert out.stat().st_size > 1000


def test_ensure_uploadable_resume_converts_md_and_caches(tmp_path: Path):
    src = tmp_path / "job_1_acme_engineer.md"
    src.write_text("Jane Doe\n\n**Python** developer\n", encoding="utf-8")
    docx = ensure_uploadable_resume(src)
    assert docx.suffix == ".docx"
    assert docx.exists()
    mtime = docx.stat().st_mtime
    again = ensure_uploadable_resume(src)
    assert again == docx
    assert again.stat().st_mtime == mtime


def test_ensure_uploadable_resume_passthrough_pdf(tmp_path: Path):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    assert ensure_uploadable_resume(pdf) == pdf
