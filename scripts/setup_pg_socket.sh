#!/usr/bin/env bash
# Expose host Postgres on a unix socket inside the project tree.
#
# Why: Cursor agent shells use an isolated network namespace. Host localhost
# (127.0.0.1:5432) is not the sandbox loopback, so TCP fails with
# "Connection refused" even when Postgres is up. A unix socket under the
# project directory is on the shared filesystem.
#
# Preferred (root): extra Postgres unix_socket_directories entry.
# Fallback (no sudo): Python proxy in the host netns → 127.0.0.1:5432
#
# Run once from a host terminal, or unsandboxed:
#   ./scripts/setup_pg_socket.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCK_DIR="$ROOT/.pg_sock"
CONF_D="${CONF_D:-/etc/postgresql/18/main/conf.d}"
DROPIN="$CONF_D/99-jobapplypro-socket.conf"
CLUSTER_VER="${CLUSTER_VER:-18}"
CLUSTER_NAME="${CLUSTER_NAME:-main}"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"
PID_FILE="$SOCK_DIR/proxy.pid"

mkdir -p "$SOCK_DIR"
chmod 1777 "$SOCK_DIR"

native_socket_ok() {
  [[ -S "$SOCK_DIR/.s.PGSQL.5432" ]] && pg_isready -h "$SOCK_DIR" -p 5432 >/dev/null 2>&1
}

configure_postgres_native() {
  [[ -d "$CONF_D" ]] || return 1
  cat > "$DROPIN" <<EOF
# JobApplyPro: extra unix socket for Cursor agent sandbox + host cron
unix_socket_directories = '/var/run/postgresql, $SOCK_DIR'
EOF
  chmod 644 "$DROPIN"
  echo "Wrote $DROPIN"
  if command -v pg_ctlcluster >/dev/null 2>&1; then
    pg_ctlcluster "$CLUSTER_VER" "$CLUSTER_NAME" reload && return 0
  fi
  local pg_pid
  pg_pid="$(pgrep -f "postgres -D .*/${CLUSTER_VER}/${CLUSTER_NAME}" | head -1 || true)"
  [[ -n "$pg_pid" ]] || return 1
  kill -HUP "$pg_pid"
}

start_proxy() {
  if ! pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
    echo "Host Postgres TCP 127.0.0.1:5432 is not reachable from this shell." >&2
    echo "Start Postgres first (this script must run in the host network namespace)." >&2
    return 1
  fi
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Proxy already running pid=$(cat "$PID_FILE")"
    return 0
  fi
  # Drop a stale socket left by a dead proxy.
  rm -f "$SOCK_DIR/.s.PGSQL.5432"
  nohup "$PYTHON" "$ROOT/scripts/pg_socket_proxy.py" "$SOCK_DIR" \
    >>"$ROOT/logs/pg_socket_proxy.log" 2>&1 &
  echo $! > "$PID_FILE"
  disown $! 2>/dev/null || true
}

echo "==== setup_pg_socket ===="
echo "SOCK_DIR=$SOCK_DIR"

if native_socket_ok; then
  echo "Socket already accepting connections."
else
  native_done=0
  if [[ "$(id -u)" -eq 0 ]] || [[ -w "$CONF_D" ]]; then
    if configure_postgres_native; then
      sleep 1
      if native_socket_ok; then
        native_done=1
        echo "Using native Postgres unix socket."
      fi
    fi
  fi
  if [[ "$native_done" -eq 0 ]]; then
    mkdir -p "$ROOT/logs"
    echo "No sudo/conf.d write — starting host TCP proxy on .pg_sock"
    start_proxy
    sleep 1
  fi
fi

if ! pg_isready -h "$SOCK_DIR" -p 5432; then
  echo "Socket still not ready at $SOCK_DIR" >&2
  exit 1
fi

echo "==== setup_pg_socket done ===="
echo "App will auto-use this socket (see utils/db_url.py)."
