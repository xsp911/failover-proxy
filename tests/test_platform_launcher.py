"""Local launcher contracts, isolated from Store imports and real listeners."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "tools" / "run-local.py"
spec = importlib.util.spec_from_file_location("run_local", LAUNCHER)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_defaults_are_separate_local_state():
    args = launcher.parser().parse_args([])
    assert args.host == "127.0.0.1" and args.port == 8843
    assert args.data_dir == Path.home() / ".failover-proxy"
    assert "9097" not in str(args.data_dir)


def test_inspection_never_creates_database(tmp_path):
    assert not launcher.admin_initialized(tmp_path)
    assert not (tmp_path / "proxy.db").exists()
    with sqlite3.connect(tmp_path / "proxy.db") as db:
        db.execute("CREATE TABLE config (payload TEXT)")
    assert not launcher.admin_initialized(tmp_path)
    with sqlite3.connect(tmp_path / "proxy.db") as db:
        db.execute("CREATE TABLE admin (singleton INTEGER PRIMARY KEY, username TEXT)")
        db.execute("INSERT INTO admin VALUES (1, 'existing')")
    assert launcher.admin_initialized(tmp_path)


def test_corrupt_existing_database_fails_closed(tmp_path):
    (tmp_path / "proxy.db").write_bytes(b"not a sqlite database")
    with pytest.raises(ValueError, match="cannot safely inspect"):
        launcher.admin_initialized(tmp_path)


@pytest.mark.parametrize("env", [{}, {"FP_ADMIN_USERNAME": "admin"},
    {"FP_ADMIN_PASSWORD": "long-enough-password"},
    {"FP_ADMIN_USERNAME": "admin", "FP_ADMIN_PASSWORD": "short"},
    {"FP_ADMIN_USERNAME": " ", "FP_ADMIN_PASSWORD": "long-enough-password"},
    {"FP_ADMIN_USERNAME": "admin", "FP_ADMIN_PASSWORD": " " * 12},
    {"FP_ADMIN_USERNAME": "user name", "FP_ADMIN_PASSWORD": "long-enough-password"}])
def test_first_init_requires_explicit_strong_credentials(env):
    with pytest.raises(ValueError):
        launcher.first_credentials(env, interactive=False)


def test_interactive_password_uses_confirmation(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "local-user")
    values = iter(["long-enough-password", "long-enough-password"])
    monkeypatch.setattr(launcher.getpass, "getpass", lambda _: next(values))
    assert launcher.first_credentials({}, interactive=True) == ("local-user", "long-enough-password")


def test_interactive_confirmation_mismatch_rejected(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "local-user")
    values = iter(["long-enough-password", "different-password"])
    monkeypatch.setattr(launcher.getpass, "getpass", lambda _: next(values))
    with pytest.raises(ValueError, match="confirmation"):
        launcher.first_credentials({}, interactive=True)


def run_isolated(tmp_path, *, existing=False, credentials=True):
    root = tmp_path / "home with spaces"
    root.mkdir()
    if existing:
        with sqlite3.connect(root / "proxy.db") as db:
            db.execute("CREATE TABLE admin (singleton INTEGER PRIMARY KEY, username TEXT)")
            db.execute("INSERT INTO admin VALUES (1, 'unchanged')")
    stub = tmp_path / "stubs"
    stub.mkdir()
    # Subprocess imports only this uvicorn stub, not application or data stores.
    (stub / "uvicorn.py").write_text("""
import json, os
from pathlib import Path
def run(app, **kwargs):
    print('RESULT=' + json.dumps({'app': app, 'kwargs': kwargs,
        'data': os.environ['FP_DATA_DIR'], 'cookie': os.environ['FP_COOKIE_NAME'],
        'username': os.environ.get('FP_ADMIN_USERNAME'),
        'password_present': bool(os.environ.get('FP_ADMIN_PASSWORD')),
        'store_imported': 'proxy.store' in __import__('sys').modules}))
""")
    env = {k: v for k, v in os.environ.items() if k not in ("FP_ADMIN_USERNAME", "FP_ADMIN_PASSWORD", "FP_DATA_DIR")}
    env["PYTHONPATH"] = str(stub)
    if credentials:
        env.update(FP_ADMIN_USERNAME="new-user", FP_ADMIN_PASSWORD="strong-secret-password")
    result = subprocess.run([sys.executable, str(LAUNCHER), "--data-dir", str(root), "--port", "8844"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
                            input="")
    return result, root


def test_launcher_subprocess_paths_factory_and_credentials(tmp_path):
    result, root = run_isolated(tmp_path)
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout.split("RESULT=", 1)[1])
    assert row["app"] == "proxy.app:create_app"
    assert row["kwargs"] == {"factory": True, "host": "127.0.0.1", "port": 8844}
    assert row["data"] == str(root.resolve()) and row["cookie"] == "fp_admin_session_local_8844"
    assert row["username"] == "new-user" and row["password_present"]
    assert not row["store_imported"] and not (root / "proxy.db").exists()
    assert "strong-secret-password" not in result.stdout + result.stderr


def test_existing_admin_no_prompt_or_environment_rotation(tmp_path):
    result, root = run_isolated(tmp_path, existing=True)
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout.split("RESULT=", 1)[1])
    assert row["username"] is None and not row["password_present"]
    with sqlite3.connect(root / "proxy.db") as db:
        assert db.execute("SELECT username FROM admin").fetchone() == ("unchanged",)


def test_missing_credentials_subprocess_does_not_create_state(tmp_path):
    result, root = run_isolated(tmp_path, credentials=False)
    assert result.returncode == 2
    assert "first initialization requires" in result.stderr
    assert not (root / "proxy.db").exists()


def test_config_validation_security_contracts():
    from proxy.config import base_url, validate_config
    for value in ("http://api.example.com", "https://127.0.0.1", "https://user:secret@example.com"):
        with pytest.raises(ValueError):
            base_url(value)
    assert base_url("https://api.example.com/v1/") == "https://api.example.com/v1"
    settings = {"request_timeout_s": 180, "connect_timeout_s": 10, "read_timeout_s": 60,
                "cooldown_s": 60, "failure_threshold": 1, "max_request_bytes": 33554432,
                "max_response_bytes": 16777216, "max_buffer_events": 32768,
                "max_concurrent_requests": 8, "max_attempts": 3, "trust_private_upstreams": False}
    clean = validate_config({"revision": 0, "settings": settings, "providers": [], "models": []})
    assert clean["settings"] == settings
    with pytest.raises(ValueError, match="unknown_settings"):
        validate_config({**clean, "settings": {**settings, "invented": True}})
