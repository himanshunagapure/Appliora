"""
Resolve DATABASE_URL for environments that cannot TCP to host localhost.

Cursor agent shells run in an isolated network namespace. Host Postgres
listens on the host loopback (127.0.0.1:5432); the agent's 127.0.0.1 is a
different interface, so connections fail with "Connection refused" even
when Postgres is healthy.

A unix socket under the project tree (.pg_sock) lives on the shared
filesystem and works from both the agent sandbox and host cron.
Create it once with:  sudo ./scripts/setup_pg_socket.sh
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOCKET_DIR = PROJECT_ROOT / ".pg_sock"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


def project_pg_socket_dir() -> Path:
    return DEFAULT_SOCKET_DIR


def pg_socket_ready(sock_dir: Path | None = None) -> bool:
    directory = Path(sock_dir or DEFAULT_SOCKET_DIR)
    return (directory / ".s.PGSQL.5432").is_socket()


def resolve_database_url(url: str, sock_dir: Path | None = None) -> str:
    """
    If DATABASE_URL points at loopback Postgres and .pg_sock exists, rewrite
    the host to that unix socket directory. Otherwise return url unchanged.
    """
    if not url or not url.startswith("postgresql"):
        return url
    directory = Path(sock_dir or DEFAULT_SOCKET_DIR)
    if not pg_socket_ready(directory):
        return url

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        return url

    userinfo = ""
    if parsed.username:
        userinfo = unquote(parsed.username)
        if parsed.password is not None:
            # urlparse may leave percent-escapes in place; normalize once.
            userinfo += ":" + quote(unquote(parsed.password), safe="")
        userinfo += "@"

    dbname = (parsed.path or "/").lstrip("/")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["host"] = str(directory)
    # Keep a dummy hostname so user:pass@ parses; libpq uses query host= for the socket.
    return f"{parsed.scheme}://{userinfo}localhost/{dbname}?{urlencode(query)}"


def sandbox_localhost_hint() -> str:
    return (
        "Postgres TCP to localhost failed. Cursor agent shells cannot reach "
        "host 127.0.0.1. Run `sudo ./scripts/setup_pg_socket.sh` once so "
        "Postgres also listens on .pg_sock (project unix socket)."
    )
