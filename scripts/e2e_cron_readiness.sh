#!/usr/bin/env bash
# End-to-end cron readiness check for JobApplyPro.
# Runs: unit tests → status → pipeline (discover→score→tailor→cover_letter) → cron script smoke.
# Does NOT submit applications.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p "$ROOT/logs"

PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

# Use project .env (Postgres). Do not inherit ad-hoc DATABASE_URL overrides.
unset DATABASE_URL
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
unset SOCKS_PROXY SOCKS5_PROXY socks_proxy socks5_proxy
export NO_PROXY='*'
export no_proxy='*'
export PYTHONUNBUFFERED=1

LOG="$ROOT/logs/e2e_cron_readiness.log"
exec > >(tee -a "$LOG") 2>&1

echo "==== e2e_cron_readiness start $(date -Is) ===="
echo "ROOT=$ROOT"
echo "PYTHON=$PYTHON"
grep -E '^(DATABASE_URL|SCORE_THRESHOLD|GEMINI_API_KEY)=' .env \
  | sed 's/\(GEMINI_API_KEY=\).*/\1***SET***/; s#://[^@]*@#://***@#'

fail=0

echo ""
echo "==== 1) Postgres ===="
SOCK_DIR="$ROOT/.pg_sock"
if [[ -S "$SOCK_DIR/.s.PGSQL.5432" ]] && pg_isready -h "$SOCK_DIR" -p 5432; then
  echo "Postgres: OK (project unix socket $SOCK_DIR)"
elif pg_isready -h 127.0.0.1 -p 5432; then
  echo "Postgres: OK (TCP localhost)"
else
  echo "Postgres: unreachable from this shell."
  echo "  Host TCP: sudo pg_ctlcluster 18 main start"
  echo "  Agent sandbox: sudo ./scripts/setup_pg_socket.sh"
  fail=1
fi

echo ""
echo "==== 2) Unit + mocked E2E tests ===="
if "$PYTHON" -m pytest tests/ -q --tb=line; then
  echo "pytest: OK"
else
  echo "pytest: FAILED"
  fail=1
fi

echo ""
echo "==== 3) cli.py status ===="
if "$PYTHON" cli.py status; then
  echo "status: OK"
else
  echo "status: FAILED"
  fail=1
fi

echo ""
echo "==== 4) Live cron-safe pipeline (discover → score → tailor → cover_letter) ===="
if "$PYTHON" cli.py pipeline; then
  echo "pipeline: OK"
else
  echo "pipeline: FAILED"
  fail=1
fi

echo ""
echo "==== 5) Status after pipeline ===="
"$PYTHON" cli.py status || true

echo ""
echo "==== 6) cron_pipeline.sh smoke ===="
if "$ROOT/scripts/cron_pipeline.sh"; then
  echo "cron_pipeline.sh: OK"
else
  echo "cron_pipeline.sh: FAILED (exit $?)"
  fail=1
fi

echo ""
echo "==== e2e_cron_readiness end $(date -Is) fail=$fail ===="
if [[ "$fail" -eq 0 ]]; then
  echo "VERDICT: READY for cron (discover→score→tailor→cover_letter; submit stays manual)."
  echo "Install with:"
  echo "  0 */6 * * * $ROOT/scripts/cron_pipeline.sh >> $ROOT/logs/cron_pipeline.log 2>&1"
else
  echo "VERDICT: NOT READY — fix failures above before enabling cron."
fi
exit "$fail"
