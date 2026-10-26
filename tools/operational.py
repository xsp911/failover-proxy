"""Private, local-only backup primitives. Never import the application or initialize a Store."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from datetime import datetime, timezone
from contextlib import closing


class OperationalError(Exception):
    """Safe diagnostic: never include database values or key contents."""


def absolute_directory(value: str, *, exists: bool = True) -> Path:
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise OperationalError('absolute_directory_required')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise OperationalError('symlink_rejected')
    if exists and not path.is_dir():
        raise OperationalError('directory_required')
    return path


def regular(path: Path) -> None:
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise OperationalError('regular_file_required')


def digest(path: Path) -> str:
    regular(path)
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def private_write(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def backup(data: str, output: str) -> dict:
    source = absolute_directory(data)
    target = absolute_directory(output, exists=False)
    if source == target or source in target.parents or target in source.parents:
        raise OperationalError('directories_must_be_disjoint')
    absolute_directory(str(target.parent))
    if target.exists():
        raise OperationalError('output_already_exists')
    entries = sorted(source.iterdir())
    if any(entry.is_symlink() for entry in entries):
        raise OperationalError('symlink_rejected')
    databases = [entry for entry in entries if entry.name.endswith('.db')]
    if not databases:
        raise OperationalError('no_databases')
    selected = databases + ([source / 'secret.key'] if (source / 'secret.key').exists() else [])
    for entry in selected:
        regular(entry)
    # Exclusive directory creation: existing backups are never overwritten or removed.
    target.mkdir(mode=0o700)
    target.chmod(0o700)
    records = []
    for entry in selected:
        destination = target / entry.name
        if entry.name.endswith('.db'):
            private_write(destination, b'')
            deadline = time.monotonic() + 60
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise OperationalError('backup_timeout')
            with closing(sqlite3.connect(entry.as_uri() + '?mode=ro', uri=True, timeout=10)) as src:
                with closing(sqlite3.connect(destination)) as dst:
                    src.backup(dst, pages=256, progress=progress, sleep=0.05)
                    # Snapshot is standalone; do not retain WAL-mode sidecar requirements.
                    dst.execute('PRAGMA journal_mode=DELETE')
            kind = 'sqlite'
        else:
            private_write(destination, entry.read_bytes())
            kind = 'fernet-key'
        records.append({'name': entry.name, 'kind': kind, 'size': destination.stat().st_size,
                        'sha256': digest(destination)})
    manifest = {'format': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                'sqlite_version': sqlite3.sqlite_version, 'files': records,
                'consistency': 'per-database SQLite online snapshots; not a cross-database transaction'}
    private_write(target / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())
    return {'ok': True, 'databases': len(databases), 'key_included': any(r['kind'] == 'fernet-key' for r in records)}


def verify(directory: str) -> dict:
    root = absolute_directory(directory)
    if root.stat().st_mode & 0o077:
        raise OperationalError('backup_directory_not_private')
    regular(root / 'manifest.json')
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('format') != 1 or not isinstance(manifest.get('files'), list):
        raise OperationalError('invalid_manifest')
    records = manifest['files']
    names = [r.get('name') for r in records]
    if not names or any(not isinstance(n, str) or Path(n).name != n or n in ('.', '..', 'manifest.json') for n in names) or len(set(names)) != len(names):
        raise OperationalError('invalid_manifest_names')
    actual = {p.name for p in root.iterdir()}
    # Read-only SQLite verification may produce sidecars on some SQLite builds; reject all unexpected files.
    if actual != set(names) | {'manifest.json'}:
        raise OperationalError('unexpected_backup_files')
    for name in ['manifest.json', *names]:
        path = root / name
        regular(path)
        if path.stat().st_mode & 0o077:
            raise OperationalError('backup_file_not_private')
    databases = []
    for record in records:
        path = root / record['name']
        if record.get('size') != path.stat().st_size or record.get('sha256') != digest(path):
            raise OperationalError('checksum_mismatch')
        if record.get('kind') == 'sqlite' and path.suffix == '.db':
            databases.append(path)
        elif record.get('kind') != 'fernet-key' or path.name != 'secret.key':
            raise OperationalError('invalid_manifest_kind')
    if not databases:
        raise OperationalError('no_databases')
    encrypted = []
    def collect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith('_encrypted') and child:
                    if not isinstance(child, str):
                        raise OperationalError('invalid_encrypted_config')
                    encrypted.append(child)
                else:
                    collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    for path in databases:
        # Completed snapshots have no WAL dependency. immutable avoids writing sidecars.
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
            if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise OperationalError('sqlite_integrity_failed')
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='config'").fetchone():
                for (payload,) in conn.execute('SELECT payload FROM config'):
                    collect(json.loads(payload))
    key_present = 'secret.key' in names
    if encrypted and not key_present:
        raise OperationalError('encrypted_config_requires_key')
    if key_present:
        from cryptography.fernet import Fernet
        cipher = Fernet((root / 'secret.key').read_bytes().strip())
        for token in encrypted:
            cipher.decrypt(token.encode())
    return {'ok': True, 'databases': len(databases), 'encrypted_values_verified': len(encrypted),
            'key_present': key_present}


def run_cli(operation, arguments) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=operation.__doc__)
    for argument in arguments:
        parser.add_argument('--' + argument.replace('_', '-'), required=True)
    options = vars(parser.parse_args())
    try:
        print(json.dumps(operation(**options), sort_keys=True))
        return 0
    except Exception as exc:
        # Deliberately do not print exception messages, paths, SQL or secret material.
        code = str(exc) if isinstance(exc, OperationalError) else 'operation_failed'
        print(json.dumps({'ok': False, 'error': code}), file=__import__('sys').stderr)
        return 1


def create_backup(data_dir: str, output_dir: str) -> dict:
    """Create a new private SQLite backup; never overwrite or restore production files."""
    return backup(data_dir, output_dir)


def verify_backup(backup_dir: str) -> dict:
    """Check backup checksums, SQLite integrity and encrypted-config decryptability."""
    return verify(backup_dir)
