#!/usr/bin/env python3
"""Online SQLite backup. Output must be an absolute, new directory."""
from operational import create_backup, run_cli

if __name__ == '__main__':
    raise SystemExit(run_cli(create_backup, ('data_dir', 'output_dir')))
