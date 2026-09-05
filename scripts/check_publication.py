"""Fail publication on private state, personal paths, or recognizable credential values.

Default scans the Git index (the exact proposed commit). --worktree scans tracked
working files, useful in CI. This is a focused guard, not a full secret scanner.
"""
import argparse
from pathlib import PurePosixPath
import re
import subprocess
import sys

PRIVATE_PARTS = {'data', 'logs', '.venv', '.zcode', '.codex', '.claude', '__pycache__'}
PRIVATE_SUFFIXES = ('.sqlite', '.sqlite3', '.db', '.log', '.pem', '.key', '.p12')
PATTERNS = [
    ('personal home path', re.compile(r'/(?:Users|home)/[A-Za-z0-9][A-Za-z0-9_.-]*/')),
    ('private key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
    ('GitHub credential', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b')),
    ('model credential', re.compile(r'\bsk-[A-Za-z0-9_-]{24,}\b')),
    ('literal credential assignment', re.compile(r'''["'](?:api_key|apiKey|access_token|password)["']\s*[:=]\s*["'][A-Za-z0-9_+/=-]{24,}["']''')),
]


def inspect(name, blob):
    path = PurePosixPath(name)
    issues = []
    if (set(path.parts) & PRIVATE_PARTS or name.endswith(PRIVATE_SUFFIXES)
            or (path.name.startswith('.env') and path.name != '.env.example')
            or path.name == 'keys.env' or '.sqlite3-' in name or '.db-' in name):
        issues.append('private runtime/configuration file')
    try:
        text = blob.decode('utf-8')
    except UnicodeDecodeError:
        return issues + ['binary file needs an explicit publication review']
    for label, pattern in PATTERNS:
        if pattern.search(text):
            issues.append(label)
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worktree', action='store_true')
    args = parser.parse_args()
    names = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
    failures, count = [], 0
    for name in filter(None, names):
        count += 1
        if args.worktree:
            from pathlib import Path
            data = Path(name).read_bytes()
        else:
            data = subprocess.check_output(['git', 'show', ':' + name])
        failures.extend((name, issue) for issue in inspect(name, data))
    if not count:
        raise SystemExit('No tracked/staged files; stage an explicit file list before publication')
    if failures:
        for name, issue in failures:
            print(f'{name}: {issue}', file=sys.stderr)  # Never print the matching sensitive value.
        raise SystemExit(1)
    print(f'Publication guard passed: {count} files; no private state or recognized credentials')


if __name__ == '__main__': main()
