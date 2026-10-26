"""Small platform adapter for persistence locks and POSIX file modes.

Windows byte-range locks preserve initializer serialization, not POSIX ACLs.
Choose a private per-user data directory on Windows; chmod is not an ACL editor.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import stat

WINDOWS = os.name == "nt"


def private_mode(fd: int, mode: int = 0o600) -> None:
    """Apply descriptor permissions where supported; never pretend to set ACLs."""
    if not WINDOWS:
        os.fchmod(fd, mode)


@contextmanager
def file_lock(fd: int):
    """Exclusive process lock; lock owners must use the same file and byte."""
    if WINDOWS:
        import msvcrt
        # CRT locks start at the current position, including beyond EOF.
        # LK_LOCK retries ten times before raising; failure remains fail-closed.
        position = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.lseek(fd, position, os.SEEK_SET)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def directory_lock(directory: Path):
    """Retain existing POSIX directory locks; use stable Windows lock file."""
    directory = Path(directory)
    if WINDOWS:
        path = directory / ".store.lock"
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("unsafe_lock_file")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
        try:
            info = os.fstat(fd)
            # Reject special files and detected replacements. A Windows ACL is
            # still required to prevent hostile writes to this directory.
            if not stat.S_ISREG(info.st_mode) or path.is_symlink() or not os.path.samestat(info, path.stat()):
                raise ValueError("unsafe_lock_file")
            with file_lock(fd):
                yield
        finally:
            os.close(fd)
    else:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with file_lock(fd):
                yield
        finally:
            os.close(fd)
