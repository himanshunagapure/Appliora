#!/usr/bin/env bash
# Phase 9 — scheduled pipeline.
# AUTO_SUBMIT=true (default in .env): discover → score → tailor → cover → fill and Submit.
# CAPTCHA / OTP / payment still stop as manual_review.
#
# Example crontab (every day at 08:00):
#   0 8 * * * /home/user/Project10/scripts/cron_pipeline.sh >> /home/user/Project10/logs/cron_pipeline.log 2>&1
#
# Or via system cron with an absolute path to this script after `chmod +x`.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Prefer project venv if present.
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  PYTHON="$(command -v python || true)"
fi

if [[ -z "${PYTHON}" || ! -x "${PYTHON}" ]]; then
  echo "cron_pipeline: no Python interpreter found (expected .venv/bin/python, python3, or python)" >&2
  echo "==== cron_pipeline end $(date -Is) exit=127 ===="
  exit 127
fi

mkdir -p "$ROOT/logs"
export PYTHONUNBUFFERED=1

echo "==== cron_pipeline start $(date -Is) ===="
set +e
"$PYTHON" "$ROOT/cli.py" pipeline
code=$?
set -e
echo "==== cron_pipeline end $(date -Is) exit=$code ===="
exit "$code"
