#!/usr/bin/env python3
"""Read-only backup verification; never logs decrypted values."""
from operational import run_cli, verify_backup

if __name__ == '__main__':
    raise SystemExit(run_cli(verify_backup, ('backup_dir',)))
