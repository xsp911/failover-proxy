#!/usr/bin/env python3
"""Local release metadata only; no application import, network, commit or push."""
import argparse
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test-result', choices=('not-run', 'passed', 'failed'), default='not-run',
                        help='Caller-supplied pytest result; this command does not run tests.')
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    version = 'unknown'
    for node in ast.parse((root / 'proxy' / '__init__.py').read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
            version = ast.literal_eval(node.value)
    def git(*arguments):
        return subprocess.check_output(['git', '-C', str(root), *arguments], text=True).strip()
    print(json.dumps({'version': version, 'git_sha': git('rev-parse', 'HEAD'),
                      'dirty': bool(git('status', '--porcelain', '--untracked-files=normal')),
                      'test_result': options.test_result, 'test_result_source': 'caller-supplied',
                      'generated_at': datetime.now(timezone.utc).isoformat()}, indent=2))


if __name__ == '__main__':
    main()
