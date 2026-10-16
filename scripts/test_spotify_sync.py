from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import spotify_sync as sync
import spotify_continue as continuation


def recording(identifier='mbid-1', title='Float On', artist='Modest Mouse', spotify_ids=(), isrcs=('US1234567890',)):
    return {'id': identifier, 'title': title, 'artist-credit': [{'name': artist, 'artist': {'id': 'artist-1', 'name': artist}}],
            'relations': [{'target-type': 'url', 'url': {'resource': 'https://open.spotify.com/track/' + value}} for value in spotify_ids],
            'length': 210000, 'isrcs': list(isrcs)}


def song(**kwargs):
    return sync.source_songs({'count': 1, 'recordings': [recording(**kwargs)]})[0]


def item(identifier='a' * 22, title='Float On', artist='Modest Mouse', isrc='US1234567890', duration=210000, added='2020-01-01T00:00:00Z'):
    return {'added_at': added, 'track': {'id': identifier, 'name': title, 'artists': [{'name': artist}],
            'duration_ms': duration, 'external_ids': {'isrc': isrc}, 'is_playable': True}}


class MatchingTests(unittest.TestCase):
    def test_metadata_link_overrides_other_liked_versions(self):
        preferred, other = 'a' * 22, 'b' * 22
        target = song(spotify_ids=[preferred])
        index = sync.LibraryIndex({'items': [item(other, added='2010-01-01T00:00:00Z'), item(preferred)]})
        match = index.existing(target)
        self.assertEqual(match['spotify_id'], preferred)
        self.assertEqual(match['method'], 'metadata_link_already_liked')
        self.assertIsNone(sync.LibraryIndex({'items': [item(other)]}).existing(target))

    def test_no_link_preserves_an_existing_live_version(self):
        live = item(title='Float On - Live at the Garden', duration=300000, isrc='DIFFERENT')
        match = sync.LibraryIndex({'items': [live]}).existing(song())
        self.assertEqual(match['spotify_id'], live['track']['id'])
        self.assertEqual(match['method'], 'existing_song_version')

    def test_isrc_collision_does_not_match_different_song(self):
        wrong = item(title='Ocean Breathes Salty')
        self.assertFalse(sync.same_song(song(), wrong['track']))
        self.assertIsNone(sync.LibraryIndex({'items': [wrong]}).existing(song()))

    def test_subtitles_and_translations_need_isrc_and_duration_support(self):
        translated = song(title='흰천장 (White Ceiling)')
        saved = item(title='White Ceiling')
        self.assertTrue(sync.same_song(translated, saved['track']))
        saved['track']['external_ids']['isrc'] = 'OTHER'
        self.assertFalse(sync.same_song(translated, saved['track']))
        saved['track']['external_ids']['isrc'] = 'US1234567890'
        saved['track']['duration_ms'] = 400000
        self.assertFalse(sync.same_song(translated, saved['track']))
        self.assertFalse(sync.same_song(song(title='A Long Song Name, Part 1'), item(title='A Long Song Name, Part 2')['track']))

    def test_cover_artist_is_not_accepted(self):
        cover = item(artist='A Different Band')
        self.assertFalse(sync.same_song(song(), cover['track']))

    def test_title_parentheses_are_preserved_unless_they_describe_an_edition(self):
        self.assertEqual(sync.base_title("Don't You (Forget About Me)"), "Don't You (Forget About Me)")
        self.assertEqual(sync.base_title('Float On (2011 Remaster)'), 'Float On')
        self.assertEqual(sync.base_title('Float On - Live at the Garden'), 'Float On')
        self.assertNotEqual(sync.normalize('?'), sync.normalize('!'))

    def test_relinked_saved_identifier_is_preserved(self):
        saved = item('a' * 22)
        saved['track']['linked_from'] = {'id': 'b' * 22}
        match = sync.LibraryIndex({'items': [saved]}).existing(song(spotify_ids=['a' * 22]))
        self.assertEqual(match['spotify_id'], 'b' * 22)

    def test_only_recording_track_links_become_preferences(self):
        raw = recording(spotify_ids=['a' * 22])
        raw['relations'].append({'target-type': 'url', 'url': {'resource': 'https://open.spotify.com/album/' + 'b' * 22}})
        raw['artist-credit'][0]['artist']['relations'] = [{'url': {'resource': 'https://open.spotify.com/track/' + 'c' * 22}}]
        result = sync.source_songs({'count': 1, 'recordings': [raw]})[0]
        self.assertEqual(result['spotify_ids'], ['a' * 22])

    def test_classical_composer_credit_does_not_substitute_a_different_performer(self):
        raw = recording(artist='Johann Sebastian Bach')
        raw['artist-credit'].append({'name': 'Glenn Gould', 'artist': {'id': 'performer', 'name': 'Glenn Gould'}})
        raw['relations'].append({'work': {'relations': [{'type': 'composer', 'artist': {'id': 'artist-1'}}]}})
        target = sync.source_songs({'count': 1, 'recordings': [raw]})[0]
        wrong = item(artist='Johann Sebastian Bach')
        wrong['track']['artists'].append({'name': 'A Different Pianist'})
        self.assertFalse(sync.same_song(target, wrong['track']))

    def test_new_matches_require_a_playable_version_that_fits_the_recording(self):
        class API:
            def request(self, endpoint, params=None, missing_ok=False):
                if endpoint == 'search':
                    return {'tracks': {'items': [item(title='Float On - Live', isrc='OTHER', duration=210000)['track']]}}
                return None
        with tempfile.TemporaryDirectory() as directory:
            catalog = sync.Catalog(API(), Path(directory), sync.LibraryIndex({'items': []}))
            self.assertEqual(catalog.resolve(song())['status'], 'unmatched')
        self.assertFalse(sync.Catalog.playable({**item()['track'], 'is_playable': False}))

    def test_metadata_relink_keeps_the_existing_saved_track(self):
        preferred, saved = 'a' * 22, 'b' * 22
        class API:
            def request(self, endpoint, params=None, missing_ok=False):
                return item(saved)['track']
        with tempfile.TemporaryDirectory() as directory:
            catalog = sync.Catalog(API(), Path(directory), sync.LibraryIndex({'items': [item(saved)]}))
            result = catalog.resolve(song(spotify_ids=[preferred]))
            self.assertEqual(result['spotify_id'], saved)
            self.assertEqual(result['method'], 'metadata_link_relinked_already_liked')

    def test_new_metadata_relink_uses_the_canonical_playable_identifier(self):
        class API:
            def request(self, *_args, **_kwargs):
                return item('b' * 22)['track']
        with tempfile.TemporaryDirectory() as directory:
            catalog = sync.Catalog(API(), Path(directory), sync.LibraryIndex({'items': []}))
            result = catalog.resolve(song(spotify_ids=['a' * 22]))
        self.assertEqual(result['spotify_id'], 'b' * 22)
        self.assertEqual(result['metadata_spotify_id'], 'a' * 22)

    def test_cached_only_cannot_request_missing_catalog_data(self):
        api = Mock()
        with tempfile.TemporaryDirectory() as directory:
            catalog = sync.Catalog(api, Path(directory), sync.LibraryIndex({'items': []}), cache_only=True)
            with self.assertRaises(sync.CacheMiss):
                catalog.resolve(song(spotify_ids=['a' * 22]))
            with self.assertRaises(sync.CacheMiss):
                catalog.resolve(song())
        api.request.assert_not_called()

    def test_partial_cached_plan_cannot_be_applied(self):
        api = Mock(auth={'user_id': 'user'}, calls=0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = {'account': {'id': 'user'}, 'total': 1, 'items': [item(title='Another Song')]}
            source = {'count': 1, 'songs': [song()]}
            sync.save_json(root / 'spotify-before.json.gz', backup)
            with patch.object(sync, 'load_source', return_value=source), patch('builtins.print'):
                plan = sync.make_plan(api, root, root / 'source.gz', resolve=True, cache_only=True)
            self.assertFalse(plan['catalog_resolved'])
            self.assertEqual(plan['decisions'][0]['method'], 'needs_catalog_lookup')
            with self.assertRaisesRegex(RuntimeError, 'Resolve missing'):
                sync.validate_plan(plan, source, backup, 'user')
            api.request.assert_not_called()


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.auth_path = self.root / 'auth.json'
        sync.save_json(self.auth_path, {'client_id': 'test-client', 'user_id': 'user',
            'refresh_token': 'test-refresh', 'access_token': 'test-access', 'expires_at': time.time() + 1000})

    def test_rate_limit_is_persisted_and_blocks_restarts_before_any_network(self):
        response = Mock(status=429)
        response.read.return_value = b'{}'
        response.getheader.return_value = '86400'
        api = sync.Spotify(self.auth_path)
        api.connection = Mock()
        api.connection.getresponse.return_value = response
        with self.assertRaises(sync.SpotifyCooldown):
            api.request('tracks/' + 'a' * 22)
        restarted = sync.Spotify(self.auth_path)
        restarted.connection = Mock()
        restarted.refresh = Mock()
        with self.assertRaises(sync.SpotifyCooldown):
            restarted.request('me', method='DELETE')
        restarted.connection.request.assert_not_called()
        restarted.refresh.assert_not_called()
        self.assertEqual((self.root / 'rate-limit.json').stat().st_mode & 0o777, 0o600)

    def test_continuation_waits_without_planning_or_mutations(self):
        api = sync.Spotify(self.auth_path)
        retry_at = api.set_cooldown(86400)
        with patch.object(continuation, 'make_plan') as plan, patch.object(continuation, 'apply_plan') as apply, patch('builtins.print'):
            self.assertEqual(continuation.continue_run(api, self.root, self.root / 'source.gz'), 'waiting')
        plan.assert_not_called()
        apply.assert_not_called()
        self.assertGreater(retry_at, time.time())
        self.assertEqual(sync.read_json(self.root / 'continuation-status.json')['state'], 'waiting_for_spotify')

    def test_continuation_resumes_a_journal_without_replacing_the_plan(self):
        api = Mock()
        sync.save_json(self.root / 'changes.json', {'phase': 'adding'})
        result = {'missing_desired_tracks': 0, 'extra_tracks': 0}
        with patch.object(continuation, 'make_plan') as plan, patch.object(continuation, 'apply_plan', return_value=result) as apply:
            self.assertEqual(continuation.continue_run(api, self.root, self.root / 'source.gz'), 'complete')
        plan.assert_not_called()
        apply.assert_called_once()

    def test_completed_continuation_makes_no_network_requests(self):
        api = Mock()
        sync.save_json(self.root / 'result.json', {'missing_desired_tracks': 0, 'extra_tracks': 0})
        self.assertEqual(continuation.continue_run(api, self.root, self.root / 'source.gz'), 'complete')
        api.check_cooldown.assert_not_called()


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.a, self.b, self.c = 'a' * 22, 'b' * 22, 'c' * 22
        self.source = {'count': 2, 'songs': [{'mbid': 'mb1'}, {'mbid': 'mb2'}], 'git_blob': 'source-blob'}
        self.backup = {'account': {'id': 'user'}, 'total': 2, 'items': [item(self.a), item(self.b)]}
        self.plan = {'source_count': 2, 'account': {'id': 'user'}, 'catalog_resolved': True,
            'desired_ids': [self.a, self.c], 'add_ids': [self.c], 'remove_ids': [self.b],
            'decisions': [{'mbid': 'mb1', 'spotify_id': self.a}, {'mbid': 'mb2', 'spotify_id': self.c}]}
        sync.save_json(self.root / 'source.json.gz', self.source)
        sync.save_json(self.root / 'plan.json.gz', self.plan)
        sync.save_json(self.root / 'spotify-before.json.gz', self.backup)
        class API:
            def __init__(api):
                api.auth = {'user_id': 'user', 'scope': 'user-library-read user-library-modify'}
                api.calls = []
            def profile(api):
                return {'id': 'user'}
            def request(api, endpoint, params=None, method='GET'):
                api.calls.append((method, endpoint, params))
        self.api = API()

    def test_additions_are_verified_before_extra_tracks_are_removed(self):
        after_add = {**self.backup, 'total': 3, 'items': self.backup['items'] + [item(self.c)]}
        final = {**self.backup, 'items': [item(self.a), item(self.c)]}
        with patch.object(sync, 'assert_source_current'), patch.object(sync, 'library', side_effect=[self.backup, after_add, final]), patch('builtins.print'):
            summary = sync.apply_plan(self.api, self.root)
        self.assertEqual([call[0] for call in self.api.calls], ['PUT', 'DELETE'])
        self.assertEqual(summary['spotify_count'], 2)
        self.assertTrue(summary['retained_dates_preserved'])

    def test_missing_added_tracks_block_all_removals(self):
        with patch.object(sync, 'assert_source_current'), patch.object(sync, 'library', side_effect=[self.backup, self.backup]), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'Added tracks are missing'):
                sync.apply_plan(self.api, self.root)
        self.assertEqual([call[0] for call in self.api.calls], ['PUT'])

    def test_external_library_changes_block_all_mutations(self):
        changed = {**self.backup, 'total': 3, 'items': self.backup['items'] + [item('d' * 22)]}
        with patch.object(sync, 'assert_source_current'), patch.object(sync, 'library', return_value=changed), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'changed outside this run'):
                sync.apply_plan(self.api, self.root)
        self.assertFalse(self.api.calls)

    def test_unresolved_plan_and_wrong_account_are_rejected(self):
        self.plan['catalog_resolved'] = False
        with self.assertRaisesRegex(RuntimeError, 'Resolve missing'):
            sync.validate_plan(self.plan, self.source, self.backup, 'user')
        self.plan['catalog_resolved'] = True
        with self.assertRaisesRegex(RuntimeError, 'same account'):
            sync.validate_plan(self.plan, self.source, self.backup, 'other-user')

    def test_interrupted_partial_batch_can_be_recovered_without_other_changes(self):
        before = {self.a, self.b}
        journal = {'added': [], 'removed': [], 'inflight': {'method': 'PUT', 'ids': [self.c, 'd' * 22]}}
        sync.recover_inflight(before, before | {self.c}, journal)
        self.assertEqual(journal['added'], [self.c])
        self.assertIsNone(journal['inflight'])
        with self.assertRaisesRegex(RuntimeError, 'changed outside this run'):
            sync.recover_inflight(before, before | {self.c, 'x' * 22}, journal)

    def test_final_verification_checks_exact_membership_and_saved_dates(self):
        wrong_dates = {**self.backup, 'items': [item(self.a, added='2026-01-01T00:00:00Z'), item(self.c)]}
        with self.assertRaisesRegex(RuntimeError, 'changed saved date'):
            sync.verify_snapshot(self.plan, self.backup, wrong_dates)
        with self.assertRaisesRegex(RuntimeError, 'extra tracks'):
            sync.verify_snapshot(self.plan, self.backup, self.backup)

    def test_resume_after_final_verification_cooldown_needs_only_one_scan(self):
        after_add = {**self.backup, 'total': 3, 'items': self.backup['items'] + [item(self.c)]}
        final = {**self.backup, 'items': [item(self.a), item(self.c)]}
        with patch.object(sync, 'assert_source_current'), patch.object(sync, 'library', side_effect=[self.backup, after_add, sync.SpotifyCooldown(time.time() + 86400)]), patch('builtins.print'):
            with self.assertRaises(sync.SpotifyCooldown):
                sync.apply_plan(self.api, self.root)
        self.assertEqual(sync.read_json(self.root / 'changes.json')['phase'], 'verifying')
        self.api.calls.clear()
        with patch.object(sync, 'assert_source_current'), patch.object(sync, 'library', return_value=final) as scan, patch('builtins.print'):
            result = sync.apply_plan(self.api, self.root)
        scan.assert_called_once()
        self.assertFalse(self.api.calls)
        self.assertEqual(result['missing_desired_tracks'], 0)


if __name__ == '__main__':
    unittest.main()
