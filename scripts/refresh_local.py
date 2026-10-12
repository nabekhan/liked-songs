#!/usr/bin/env python3
"""Fast-forward this local mirror without replacing any local edits."""
import datetime
import fcntl
from pathlib import Path
import subprocess
import sys
from sync import materialize_compressed_exports

ROOT = Path(__file__).resolve().parents[1]

def main():
    with (ROOT / '.sync.lock').open('w') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        def git(*args):
            return subprocess.run(['git', '-C', str(ROOT), *args], text=True, capture_output=True, check=True)
        if git('status', '--porcelain').stdout.strip():
            print('Local refresh skipped: checkout has uncommitted changes.'); return
        if git('branch', '--show-current').stdout.strip() != 'main':
            print('Local refresh skipped: checkout is not on main.'); return
        git('pull', '--ff-only', '--no-rebase', 'origin', 'main')
        materialize_compressed_exports()
        print(datetime.datetime.now().isoformat(), 'Local copy refreshed from GitHub.')

if __name__ == '__main__':
    try: main()
    except subprocess.CalledProcessError:
        print('Local refresh failed; checkout was preserved.', file=sys.stderr); sys.exit(1)
