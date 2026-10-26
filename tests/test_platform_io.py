"""Portable lock contracts, including process-level serialization."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from proxy import platform_io

ROOT = Path(__file__).resolve().parents[1]


def test_directory_lock_serializes_subprocess_initializers(tmp_path):
    script = """
import sys
from pathlib import Path
from proxy.platform_io import directory_lock
root = Path(sys.argv[1])
for _ in range(20):
    with directory_lock(root):
        p = root / 'counter'
        value = int(p.read_text()) if p.exists() else 0
        p.write_text(str(value + 1))
"""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    children = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
    for child in children:
        stdout, stderr = child.communicate(timeout=45)
        assert child.returncode == 0, (stdout, stderr)
    assert (tmp_path / "counter").read_text() == "60"


def test_lock_released_after_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with platform_io.directory_lock(tmp_path):
            raise RuntimeError("test")
    with platform_io.directory_lock(tmp_path):
        pass


def test_windows_byte_lock_seeks_and_unlocks(monkeypatch, tmp_path):
    calls = []
    mock = SimpleNamespace(LK_LOCK=1, LK_UNLCK=0,
                           locking=lambda fd, op, size: calls.append((op, size, os.lseek(fd, 0, os.SEEK_CUR))))
    monkeypatch.setitem(sys.modules, "msvcrt", mock)
    monkeypatch.setattr(platform_io, "WINDOWS", True)
    fd = os.open(tmp_path / "file", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.lseek(fd, 7, os.SEEK_SET)
        with pytest.raises(RuntimeError):
            with platform_io.file_lock(fd):
                os.lseek(fd, 9, os.SEEK_SET)
                raise RuntimeError("test")
        assert calls == [(1, 1, 0), (0, 1, 0)]
        assert os.lseek(fd, 0, os.SEEK_CUR) == 7
    finally:
        os.close(fd)


def test_windows_directory_lock_uses_regular_stable_file(monkeypatch, tmp_path):
    calls = []
    mock = SimpleNamespace(LK_LOCK=1, LK_UNLCK=0,
                           locking=lambda fd, op, size: calls.append(op))
    monkeypatch.setitem(sys.modules, "msvcrt", mock)
    monkeypatch.setattr(platform_io, "WINDOWS", True)
    with platform_io.directory_lock(tmp_path):
        assert (tmp_path / ".store.lock").is_file()
    assert calls == [1, 0]


def test_windows_rejects_nonregular_lock_path(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_io, "WINDOWS", True)
    (tmp_path / ".store.lock").mkdir()
    with pytest.raises(ValueError, match="unsafe_lock_file"):
        with platform_io.directory_lock(tmp_path):
            pytest.fail("unsafe lock entered")


def test_private_mode_preserves_posix_permissions(tmp_path):
    fd = os.open(tmp_path / "file", os.O_CREAT | os.O_RDWR, 0o666)
    try:
        platform_io.private_mode(fd)
        if os.name != "nt":
            assert os.fstat(fd).st_mode & 0o777 == 0o600
    finally:
        os.close(fd)


def test_private_mode_windows_does_not_call_posix_api(monkeypatch):
    monkeypatch.setattr(platform_io, "WINDOWS", True)
    def fail(*args):
        pytest.fail("Windows must not use os.fchmod")
    monkeypatch.setattr(os, "fchmod", fail, raising=False)
    platform_io.private_mode(123)
