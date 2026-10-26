#!/usr/bin/env python3
"""Safe local entry point; no service, dependency, or production-data changes."""
from __future__ import annotations

import argparse
from contextlib import closing
import getpass
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8843)
    result.add_argument("--data-dir", type=Path, default=Path.home() / ".failover-proxy")
    return result


def admin_initialized(data_dir: Path) -> bool:
    """Read existing state without creating a DB or initializing any Store."""
    db = data_dir / "proxy.db"
    if not db.exists():
        return False
    if not db.is_file() or db.is_symlink():
        raise ValueError("unsafe database path")
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin'"
            ).fetchone()
            return bool(exists and connection.execute(
                "SELECT 1 FROM admin WHERE singleton=1"
            ).fetchone())
    except sqlite3.Error as exc:
        raise ValueError("cannot safely inspect existing database; no credentials changed") from exc


def first_credentials(environ, *, interactive: bool) -> tuple[str, str]:
    username = environ.get("FP_ADMIN_USERNAME", "")
    password = environ.get("FP_ADMIN_PASSWORD", "")
    if bool(username) != bool(password):
        raise ValueError("set both FP_ADMIN_USERNAME and FP_ADMIN_PASSWORD")
    if not username and not password:
        if not interactive:
            raise ValueError("first initialization requires FP_ADMIN_USERNAME and FP_ADMIN_PASSWORD, or an interactive terminal")
        username = input("New admin username: ").strip()
        password = getpass.getpass("New admin password (at least 12 characters): ")
        if password != getpass.getpass("Confirm admin password: "):
            raise ValueError("password confirmation does not match")
    if not username.strip() or len(username) > 100 or any(ord(c) < 33 or ord(c) == 127 for c in username):
        raise ValueError("admin username must contain 1–100 printable non-space characters")
    if not 12 <= len(password) <= 1024 or password.isspace():
        raise ValueError("admin password must contain 12–1024 characters")
    return username, password


def verify_runtime() -> None:
    if sys.version_info < (3, 12):
        raise ValueError("Python 3.12 or newer is required")


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if not args.host.strip() or not 1 <= args.port <= 65535:
            raise ValueError("host must be nonempty; port must be between 1 and 65535")
        verify_runtime()
        data_dir = args.data_dir.expanduser().resolve()
        if data_dir == Path("/opt/failover-proxy/data").resolve():
            raise ValueError("refusing the legacy production data directory; choose a separate local --data-dir")
        initialized = admin_initialized(data_dir)
        credentials = None if initialized else first_credentials(os.environ, interactive=sys.stdin.isatty())
        # Resolve dependencies before mutating the environment or creating state.
        import uvicorn
        sys.path.insert(0, str(ROOT))
        os.environ["FP_DATA_DIR"] = str(data_dir)
        os.environ["FP_COOKIE_NAME"] = "fp_admin_session_local_" + str(args.port)
        if credentials:
            os.environ["FP_ADMIN_USERNAME"], os.environ["FP_ADMIN_PASSWORD"] = credentials
        else:
            # The factory's INSERT OR IGNORE already preserves credentials. Do not
            # even request initialization when launching an existing admin DB.
            os.environ.pop("FP_ADMIN_USERNAME", None)
            os.environ.pop("FP_ADMIN_PASSWORD", None)
        print(f"Local UI: http://{args.host}:{args.port}  Data: {data_dir}", flush=True)
        uvicorn.run("proxy.app:create_app", factory=True, host=args.host, port=args.port)
        return 0
    except (ValueError, ImportError, OSError, EOFError, KeyboardInterrupt) as exc:
        print(f"run-local: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
