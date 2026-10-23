"""
Forward a project unix socket to host Postgres on 127.0.0.1:5432.

Used when we cannot add a second unix_socket_directories entry to Postgres
(no sudo). The proxy must run in the host network namespace (normal terminal
or unsandboxed agent). Cursor agent shells then connect via .pg_sock.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading
from pathlib import Path

DEFAULT_SOCK_DIR = Path(__file__).resolve().parent.parent / ".pg_sock"
SOCKET_NAME = ".s.PGSQL.5432"
PID_NAME = "proxy.pid"
TARGET = ("127.0.0.1", 5432)


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            src.shutdown(socket.SHUT_RD)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def serve(sock_dir: Path | None = None) -> None:
    directory = Path(sock_dir or DEFAULT_SOCK_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o1777)
    listen_path = directory / SOCKET_NAME
    pid_path = directory / PID_NAME

    if listen_path.exists() or listen_path.is_socket():
        listen_path.unlink()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(listen_path))
    os.chmod(listen_path, 0o777)
    srv.listen(128)
    pid_path.write_text(str(os.getpid()))

    def _shutdown(*_args):
        try:
            srv.close()
        except OSError:
            pass
        for path in (listen_path, pid_path):
            try:
                path.unlink()
            except OSError:
                pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        client, _ = srv.accept()
        try:
            remote = socket.create_connection(TARGET, timeout=10)
        except OSError:
            client.close()
            continue
        threading.Thread(target=_pipe, args=(client, remote), daemon=True).start()
        threading.Thread(target=_pipe, args=(remote, client), daemon=True).start()


if __name__ == "__main__":
    serve(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
