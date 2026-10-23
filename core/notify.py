"""
Phase 11 — optional Slack / email alerts when new jobs score at/above threshold.

No-ops when nothing is configured. Never raises into the scoring stage.
Skipped automatically under pytest unless force=True.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
from email.mime.text import MIMEText
from typing import Any, Iterable
from urllib.request import Request, urlopen

from config.settings import get_settings
from utils.logger import get_logger

logger = get_logger("jobapplypro.notify")


def notify_high_score_jobs(
    jobs: Iterable[dict[str, Any]],
    *,
    force: bool = False,
    settings=None,
) -> dict[str, Any]:
    """
    Send a digest for newly scored high-fit jobs.

    Each job dict: id, title, company, score, url, source.
    """
    items = [j for j in jobs if j]
    summary = {"sent": 0, "skipped": None, "errors": []}
    if not items:
        summary["skipped"] = "empty"
        return summary
    if not force and os.environ.get("PYTEST_CURRENT_TEST"):
        summary["skipped"] = "pytest"
        return summary

    settings = settings or get_settings()
    webhook = (getattr(settings, "notify_slack_webhook_url", None) or "").strip()
    email_to = (getattr(settings, "notify_email_to", None) or "").strip()
    if not webhook and not email_to:
        summary["skipped"] = "unconfigured"
        logger.debug("High-score notify skipped — no Slack webhook or email to.")
        return summary

    body = _format_digest(items, float(getattr(settings, "score_threshold", 6.0)))
    if webhook:
        try:
            _post_slack(webhook, body)
            summary["sent"] += 1
            logger.info("Slack high-score alert sent for %d job(s).", len(items))
        except Exception as exc:
            summary["errors"].append(f"slack: {exc}")
            logger.warning("Slack notify failed: %s", exc)

    if email_to:
        try:
            _send_email(settings, email_to, body, n=len(items))
            summary["sent"] += 1
            logger.info("Email high-score alert sent for %d job(s) → %s", len(items), email_to)
        except Exception as exc:
            summary["errors"].append(f"email: {exc}")
            logger.warning("Email notify failed: %s", exc)

    return summary


def _format_digest(jobs: list[dict[str, Any]], threshold: float) -> str:
    lines = [
        f"JobApplyPro: {len(jobs)} new job(s) scored ≥ {threshold:g}",
        "",
    ]
    for job in jobs:
        score = job.get("score")
        score_s = f"{score:.1f}" if isinstance(score, (int, float)) else "?"
        lines.append(
            f"• [{score_s}] {job.get('title') or '?'} @ {job.get('company') or '?'} "
            f"({job.get('source') or '?'}) id={job.get('id')}"
        )
        if job.get("url"):
            lines.append(f"  {job['url']}")
    lines.append("")
    lines.append("Next: python cli.py pipeline  (or tailor/cover_letter) then review.")
    return "\n".join(lines)


def _post_slack(webhook: str, text: str) -> None:
    if not webhook.lower().startswith(("https://", "http://")):
        raise RuntimeError("NOTIFY_SLACK_WEBHOOK_URL must be an http(s) URL")
    payload = json.dumps({"text": text[:3900]}).encode("utf-8")
    req = Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=15) as resp:
        resp.read()


def _send_email(settings, email_to: str, body: str, *, n: int) -> None:
    host = (getattr(settings, "notify_smtp_host", None) or "").strip()
    if not host:
        raise RuntimeError("NOTIFY_SMTP_HOST is required for email alerts")
    port = int(getattr(settings, "notify_smtp_port", 587) or 587)
    user = (getattr(settings, "notify_smtp_user", None) or "").strip()
    password = getattr(settings, "notify_smtp_password", None) or ""
    mail_from = (getattr(settings, "notify_email_from", None) or user or email_to).strip()

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"JobApplyPro: {n} high-score job(s)"
    msg["From"] = mail_from
    msg["To"] = email_to

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.ehlo()
        try:
            smtp.starttls(context=context)
            smtp.ehlo()
        except smtplib.SMTPNotSupportedError:
            pass
        if user:
            smtp.login(user, password)
        smtp.sendmail(mail_from, [addr.strip() for addr in email_to.split(",") if addr.strip()], msg.as_string())
