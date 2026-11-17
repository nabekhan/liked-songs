import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

import sync_listenbrainz as lb

CONFIG = {'collection_id': '4cad0217-1cbe-43ae-b148-afa5afad7625',
          'owner': 'nabecite', 'user_agent': 'LikedSongsTests/1.0 (https://example.org)'}
A, B, C, D = [f'00000000-0000-4000-8000-{n:012d}' for n in range(1, 5)]


class FakeAPI:
    def __init__(self, scores=None):
        self.scores = dict(scores or {})
        self.calls = []
        self.fail = None

    def validate_token(self):
        self.calls.append('validate')

    def feedback(self):
        return dict(self.scores)

    def set_score(self, mbid, score):
        self.calls.append((mbid, score))
        if self.fail:
            raise self.fail
        self.scores[mbid] = score


class ReconcileTests(unittest.TestCase):
    def state(self, ids=()):
        return dict(lb.state_identity(CONFIG), managed_recording_mbids=list(ids))

    def run_sync(self, api, desired, state=None, **kwargs):
        saved = []
        result = lb.reconcile(api, CONFIG, desired, state or self.state(),
                              lambda value: saved.append(copy.deepcopy(value)), **kwargs)
        return result, saved

    def test_dry_run_does_not_write_feedback_or_state(self):
        api = FakeAPI({A: 1, B: 1})
        result, saved = self.run_sync(api, [C], self.state([A]))
        self.assertEqual((result['additions'], result['removals']), (1, 1))
        self.assertEqual(api.calls, [])
        self.assertEqual(saved, [])

    def test_add_remove_preserve_unrelated_loves_and_explicit_hates(self):
        api = FakeAPI({A: 1, B: -1, C: 1})
        result, saved = self.run_sync(api, [D], self.state([A, B]), apply=True)
        self.assertEqual(api.calls, ['validate', (A, 0), (D, 1)])
        self.assertEqual(api.scores[B], -1)
        self.assertEqual(api.scores[C], 1)
        self.assertEqual(result['pending'], 0)
        self.assertEqual(saved[-1]['managed_recording_mbids'], [D])

    def test_source_reasserts_love_for_current_collection_entries(self):
        api = FakeAPI({A: -1})
        self.run_sync(api, [A], apply=True)
        self.assertEqual(api.calls, ['validate', (A, 1)])

    def test_existing_loves_are_adopted_and_repeated_runs_are_noops(self):
        api = FakeAPI({A: 1})
        _, saved = self.run_sync(api, [A], apply=True)
        self.assertEqual(saved[-1]['managed_recording_mbids'], [A])
        self.run_sync(api, [A], saved[-1], apply=True)
        self.assertEqual(api.calls, ['validate', 'validate'])

    def test_first_run_does_not_remove_unrelated_existing_loves(self):
        api = FakeAPI({B: 1})
        self.run_sync(api, [A], apply=True)
        self.assertNotIn((B, 0), api.calls)

    def test_write_limit_resumes_from_actual_feedback(self):
        api = FakeAPI()
        result, saved = self.run_sync(api, [A, B], apply=True, max_writes=1)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(saved[0]['managed_recording_mbids'], [A, B])
        result, _ = self.run_sync(api, [A, B], saved[-1], apply=True)
        self.assertEqual(result['applied'], 1)
        self.assertEqual(api.calls, ['validate', (A, 1), 'validate', (B, 1)])

    def test_failed_or_ambiguous_write_can_be_reconciled_after_source_removal(self):
        api = FakeAPI()
        api.fail = RuntimeError('Connection lost after server accepted request')
        saved = []
        with self.assertRaises(RuntimeError):
            lb.reconcile(api, CONFIG, [A], self.state(), lambda v: saved.append(copy.deepcopy(v)), apply=True)
        self.assertEqual(saved[0]['managed_recording_mbids'], [A])
        # Simulate that the server accepted the love despite the lost response.
        api.fail = None
        api.scores[A] = 1
        self.run_sync(api, [], saved[0], apply=True)
        self.assertEqual(api.scores[A], 0)

    def test_remote_checkpoint_must_succeed_before_any_target_write(self):
        api = FakeAPI()
        with self.assertRaisesRegex(RuntimeError, 'push failed'):
            self.run_sync(api, [A], apply=True, publish=Mock(side_effect=RuntimeError('push failed')))
        self.assertEqual(api.calls, ['validate'])

    def test_initial_remote_checkpoint_contains_all_intended_additions(self):
        api = FakeAPI()
        saved, events = [], []
        def save(value):
            saved.append(copy.deepcopy(value))
        def publish():
            events.append((len(api.calls), saved[-1]['managed_recording_mbids']))
        lb.reconcile(api, CONFIG, [A, B], self.state(), save, apply=True, publish=publish)
        self.assertEqual(events[0], (1, [A, B]))
        self.assertEqual(len(events), 2)

    def test_budget_stops_cleanly_with_owned_pending_work(self):
        api = FakeAPI()
        api.fail = lb.BudgetExpired()
        result, saved = self.run_sync(api, [A], apply=True)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(saved[-1]['managed_recording_mbids'], [A])

    def test_invalid_or_duplicate_source_ids_fail_before_api_calls(self):
        for ids in ([A, A], ['not-an-mbid']):
            api = FakeAPI()
            with self.assertRaises(ValueError):
                self.run_sync(api, ids, apply=True)
            self.assertEqual(api.calls, [])

    def test_mismatched_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            state = self.state([A])
            state['collection_id'] = 'different-collection'
            path.write_text(json.dumps(state))
            with self.assertRaises(ValueError):
                lb.load_state(path, CONFIG)

    def test_weekly_snapshot_checks_identity_count_checksum_and_compression(self):
        snapshot = {'collection_id': CONFIG['collection_id'], 'collection_owner': CONFIG['owner'],
                    'recordings': [{'id': B}, {'id': A}], 'count': 2,
                    'membership_sha256': lb.hashlib.sha256((A + '\n' + B).encode()).hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'collection.json.gz'
            with lb.gzip.open(path, 'wt', encoding='utf-8') as output:
                json.dump(snapshot, output)
            self.assertEqual(lb.snapshot_ids(path, CONFIG), [A, B])
            for changes in ({'count': 1}, {'membership_sha256': 'bad'},
                            {'collection_owner': 'someone-else'}, {'recordings': [{'id': A}, {'id': A}]}):
                path = Path(directory) / 'collection.json'
                path.write_text(json.dumps(dict(snapshot, **changes)))
                with self.assertRaises(ValueError):
                    lb.snapshot_ids(path, CONFIG)


class APIBehaviorTests(unittest.TestCase):
    def api(self):
        return lb.ListenBrainz(CONFIG, 'fake-test-token')

    def page(self, rows, total=None, offset=0):
        return {'total_count': len(rows) if total is None else total, 'offset': offset, 'feedback': rows}

    def row(self, mbid=A, score=1):
        return {'recording_mbid': mbid, 'recording_msid': None, 'score': score}

    def test_paginated_feedback_reads_all_rows_without_metadata(self):
        api = self.api()
        api.request = Mock(side_effect=[self.page([self.row()], 2), self.page([self.row(B, -1)], 2, 1)])
        self.assertEqual(api.feedback(), {A: 1, B: -1})
        urls = [call.args[0] for call in api.request.call_args_list]
        self.assertTrue(all('metadata=false' in url and 'count=1000' in url for url in urls))
        self.assertIn('offset=1', urls[1])

    def test_incomplete_changing_or_overlapping_feedback_is_rejected(self):
        first = self.page([self.row()], 2)
        for second in (self.page([], 2, 1), self.page([self.row(B)], 3, 1),
                       self.page([self.row()], 2, 1), self.page([self.row(B)], 2, 0)):
            api = self.api()
            api.request = Mock(side_effect=[first, second])
            with self.assertRaises(ValueError):
                api.feedback()

    def test_msid_only_feedback_is_not_guessed_or_matched_by_name(self):
        api = self.api()
        api.request = Mock(return_value=self.page([{'recording_mbid': None, 'recording_msid': B, 'score': 1}]))
        self.assertEqual(api.feedback(), {})

    def test_wrong_token_identity_stops_before_feedback_or_writes(self):
        api = self.api()
        api.request = Mock(return_value={'valid': True, 'user_name': 'someone-else'})
        with self.assertRaises(ValueError):
            lb.reconcile(api, CONFIG, [A], dict(lb.state_identity(CONFIG), managed_recording_mbids=[]), Mock(), apply=True)
        api.request.assert_called_once_with('validate-token')

    def test_retry_after_and_transient_timeout_are_retried(self):
        api = self.api()
        response = io.BytesIO(b'{"status":"ok"}')
        response.headers = {}
        error = urllib.error.HTTPError(lb.API, 429, 'slow down', {'Retry-After': '4'}, None)
        with patch.object(lb.urllib.request, 'urlopen', side_effect=[error, socket_timeout(), response]) as send, patch.object(api, 'wait') as wait:
            api.set_score(A, 1)
        self.assertEqual(send.call_count, 3)
        self.assertIn(((5,), {}), [(c.args, c.kwargs) for c in wait.call_args_list])

    def test_requests_identify_client_and_errors_do_not_expose_token(self):
        api = self.api()
        error = urllib.error.HTTPError(lb.API, 401, 'fake-test-token', {}, None)
        with patch.object(lb.urllib.request, 'urlopen', side_effect=error) as send:
            with self.assertRaisesRegex(RuntimeError, '^ListenBrainz HTTP 401.$'):
                api.set_score(A, 1)
        request = send.call_args.args[0]
        self.assertIn('LikedSongsTests', request.get_header('User-agent'))
        self.assertEqual(json.loads(request.data), {'recording_mbid': A, 'score': 1})


def socket_timeout():
    return lb.socket.timeout('timeout')


class StateBranchTests(unittest.TestCase):
    def test_checkpoints_touch_only_dedicated_branch_and_have_history(self):
        script = Path(lb.__file__).with_name('publish_listenbrainz_state.sh')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args, cwd=root):
                return subprocess.check_output(['git', *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
            git('init', '--bare', 'remote.git')
            git('init', 'repo')
            repo = root / 'repo'
            git('config', 'user.name', 'Test', cwd=repo)
            git('config', 'user.email', 'test@example.org', cwd=repo)
            (repo / 'README').write_text('existing main')
            git('add', 'README', cwd=repo)
            git('commit', '-m', 'Initial', cwd=repo)
            git('branch', '-M', 'main', cwd=repo)
            git('remote', 'add', 'origin', str(root / 'remote.git'), cwd=repo)
            git('push', 'origin', 'main', cwd=repo)
            main = git('rev-parse', 'main', cwd=repo)
            (repo / '.cache').mkdir()
            state = repo / '.cache/state.json'
            for n in (1, 2):
                state.write_text(json.dumps({'checkpoint': n}))
                subprocess.run(['bash', str(script), str(state)], cwd=repo, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.assertEqual(git('rev-parse', 'main', cwd=repo), main)
            branch = 'refs/heads/listenbrainz-sync-state'
            self.assertEqual(git('--git-dir=remote.git', 'ls-tree', '--name-only', branch), 'state.json')
            self.assertEqual(json.loads(git('--git-dir=remote.git', 'show', branch + ':state.json')), {'checkpoint': 2})
            self.assertEqual(git('--git-dir=remote.git', 'rev-list', '--count', branch), '2')
            # A competing checkpoint must not be overwritten by our stale parent.
            head = git('--git-dir=remote.git', 'rev-parse', branch)
            tree = git('--git-dir=remote.git', 'rev-parse', branch + '^{tree}')
            competitor = git('-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                             'commit-tree', tree, '-p', head, '-m', 'Competing checkpoint', cwd=repo)
            git('push', 'origin', competitor + ':' + branch, cwd=repo)
            state.write_text(json.dumps({'checkpoint': 3}))
            result = subprocess.run(['bash', str(script), str(state)], cwd=repo,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(git('--git-dir=remote.git', 'rev-parse', branch), competitor)


if __name__ == '__main__':
    unittest.main()
