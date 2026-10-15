import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import schedule_sync

UTC = dt.timezone.utc
REPOSITORY = 'nabekhan/liked-songs'
SUNDAY = dt.datetime(2026, 10, 18, tzinfo=UTC)


class ScheduleTests(unittest.TestCase):
    def test_slot_is_stable_within_week_and_varies_between_weeks(self):
        slot = schedule_sync.weekly_slot(REPOSITORY, SUNDAY)
        self.assertEqual(slot, schedule_sync.weekly_slot(REPOSITORY, SUNDAY + dt.timedelta(days=6, hours=23)))
        self.assertEqual(slot.date(), SUNDAY.date())
        self.assertEqual(slot.minute, 43)
        hours = {schedule_sync.weekly_slot(REPOSITORY, SUNDAY + dt.timedelta(weeks=i)).hour for i in range(26)}
        self.assertGreater(len(hours), 1)

    def test_hourly_checks_sync_only_once_per_week(self):
        for week in range(4):
            start = SUNDAY + dt.timedelta(weeks=week)
            status = {}; runs = []
            for hour in range(24):
                now = start + dt.timedelta(hours=hour, minutes=43)
                if schedule_sync.sync_due(REPOSITORY, status, now):
                    runs.append(now)
                    status = {'checked_at': now.isoformat()}
            self.assertEqual(runs, [schedule_sync.weekly_slot(REPOSITORY, start)])

    def test_not_due_before_slot_and_due_at_slot(self):
        slot = schedule_sync.weekly_slot(REPOSITORY, SUNDAY)
        old = {'checked_at': (SUNDAY - dt.timedelta(days=7)).isoformat()}
        self.assertFalse(schedule_sync.sync_due(REPOSITORY, old, slot - dt.timedelta(seconds=1)))
        self.assertTrue(schedule_sync.sync_due(REPOSITORY, old, slot))

    def test_missed_or_failed_slot_can_retry_later(self):
        slot = schedule_sync.weekly_slot(REPOSITORY, SUNDAY)
        self.assertTrue(schedule_sync.sync_due(REPOSITORY, {}, slot + dt.timedelta(hours=1)))

    def test_successful_manual_sync_also_counts_for_this_week(self):
        slot = schedule_sync.weekly_slot(REPOSITORY, SUNDAY)
        self.assertFalse(schedule_sync.sync_due(REPOSITORY, {'checked_at': SUNDAY.isoformat()}, slot))

    def test_utc_week_boundary_and_year_rollover(self):
        local = dt.datetime(2027, 1, 2, 18, tzinfo=dt.timezone(dt.timedelta(hours=-7)))
        self.assertEqual(schedule_sync.week_start(local), dt.datetime(2027, 1, 3, tzinfo=UTC))

    def test_manual_and_push_runs_bypass_schedule_without_status_file(self):
        for event in ('workflow_dispatch', 'push'):
            with tempfile.TemporaryDirectory() as t:
                output = Path(t) / 'output'
                env = {'GITHUB_EVENT_NAME': event, 'GITHUB_OUTPUT': str(output)}
                with patch.dict(os.environ, env), patch.object(schedule_sync, 'ROOT', Path(t)):
                    schedule_sync.main()
                self.assertEqual(output.read_text(), 'run_sync=true\n')

    def test_scheduled_entrypoint_reads_success_marker_and_skips(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); (root / 'data').mkdir()
            # A marker later than the current check must also prevent duplicates.
            marker = dt.datetime.now(UTC) + dt.timedelta(minutes=1)
            (root / 'data/sync-status.json').write_text(json.dumps({'checked_at': marker.isoformat()}))
            output = root / 'output'
            env = {'GITHUB_EVENT_NAME': 'schedule', 'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_OUTPUT': str(output)}
            with patch.dict(os.environ, env), patch.object(schedule_sync, 'ROOT', root):
                schedule_sync.main()
            self.assertEqual(output.read_text(), 'run_sync=false\n')


if __name__ == '__main__': unittest.main()
