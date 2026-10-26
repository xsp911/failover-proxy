#!/usr/bin/env python3
"""Offline stdlib-only heuristic scan. Reports locations/rules, never matched values."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

# Deliberately no generic long-string/hash rule: hashes, model data and fixtures are not credentials.
RULES = {
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{50,255})\b'),
    'aws-access-id': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'slack-token': re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{24,}\b'),
    'provider-token': re.compile(r'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}\b'),
}
SKIP_PARTS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'data', 'backups', 'dist', 'build'}
SKIP_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.pyc', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.woff', '.woff2', '.zip', '.gz', '.pdf'}


def scan(root):
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], capture_output=True, check=True)
    findings = []
    scanned = 0
    for name in sorted(set(result.stdout.decode('utf-8', 'surrogateescape').split('\0')) - {''}):
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            continue
        if any(p in SKIP_PARTS for p in relative.parts) or relative.suffix.lower() in SKIP_SUFFIXES:
            continue
        if relative.name == '.env' or relative.name.endswith('.env') or relative.name.startswith('.env.') or relative.name == 'secret.key' or relative.name.endswith(('-wal', '-shm')):
            continue
        path = root / relative
        if any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file():
            continue
        raw = path.read_bytes()
        if b'\x00' in raw:
            continue
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            continue
        scanned += 1
        for number, line in enumerate(text.splitlines(), 1):
            if 'secret-check: allow synthetic' in line:
                continue
            for rule, pattern in RULES.items():
                if pattern.search(line):
                    findings.append({'file': name, 'line': number, 'rule': rule})
    return {'ok': not findings, 'scanned_files': scanned, 'findings': findings,
            'scope': 'tracked and nonignored untracked source; heuristic, not a credential audit'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    options = parser.parse_args()
    try:
        result = scan(options.root.resolve())
    except Exception:
        print(json.dumps({'ok': False, 'error': 'scan_failed'}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
