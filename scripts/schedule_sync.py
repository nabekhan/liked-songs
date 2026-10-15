#!/usr/bin/env python3
"""Choose one Sunday sync slot per week, without contacting MusicBrainz."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def week_start(now):
    now = now.astimezone(dt.timezone.utc)
    return (now - dt.timedelta(days=(now.weekday() + 1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)


def weekly_slot(repository, now):
    start = week_start(now)
    # Stable within a week so retries and delayed runs keep the same slot.
    seed = f'{repository}:{start.date().isoformat()}'.encode()
    hour = int.from_bytes(hashlib.sha256(seed).digest(), 'big') % 24
    return start + dt.timedelta(hours=hour, minutes=43)


def sync_due(repository, status, now):
    checked_at = status.get('checked_at')
    if checked_at:
        checked = dt.datetime.fromisoformat(checked_at)
        if checked >= week_start(now): return False
    # A delayed or failed scheduled run can catch up at a later hourly check.
    return now >= weekly_slot(repository, now)


def main():
    run_sync = True
    if os.environ['GITHUB_EVENT_NAME'] == 'schedule':
        now = dt.datetime.now(dt.timezone.utc)
        path = ROOT / 'data/sync-status.json'
        status = json.loads(path.read_text()) if path.exists() else {}
        repository = os.environ['GITHUB_REPOSITORY']
        run_sync = sync_due(repository, status, now)
        print('Weekly UTC slot:', weekly_slot(repository, now).isoformat())
    value = str(run_sync).lower()
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write(f'run_sync={value}\n')
    print('Run sync:', value)


if __name__ == '__main__': main()
