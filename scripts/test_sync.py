import csv
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sync
import datetime as dt

CID = '4cad0217-1cbe-43ae-b148-afa5afad7625'
CONFIG = {'collection_id': CID, 'owner': 'nabecite', 'name': 'Liked Songs'}
METADATA = {'id': CID, 'editor': 'nabecite', 'entity-type': 'recording', 'recording-count': 2, 'name': 'Liked Songs'}
ROWS = [
    {'id': 'fc8fc63e-9a33-4f98-b6ea-c3ed82515996', 'title': 'NOW OR NEVER', 'length': 123428,
     'artist-credit': [{'name': 'Tkandz', 'artist': {'id': '8ceae8a6-07f8-4d0a-a317-074398260dd3'}, 'joinphrase': ' & '}, {'name': 'CXSPER', 'artist': {'id': '4724214c-d190-4db4-a49e-fcdfb3a0db5b'}}], 'isrcs': ['TEST123', 'TEST123']},
    {'id': '973da94f-0546-4846-9f9f-b37730750249', 'title': '</script><script>alert(1)</script>'},
]

class FakeAPI:
    def __init__(self, responses): self.responses = iter(responses); self.calls = []
    @property
    def request_count(self): return len(self.calls)
    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return next(self.responses)

class SyncTests(unittest.TestCase):
    def cached_data(self, rows):
        return {'collection_id': CID, 'collection_owner': 'nabecite', 'collection_name': 'Liked Songs', 'synced_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'metadata_refreshed_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'collection_metadata': METADATA, 'metadata_includes': sync.METADATA_INCLUDES, 'recordings': sorted(rows, key=lambda r: r['id']), 'count': len(rows)}

    def test_no_change_leaves_large_files_untouched(self):
        with tempfile.TemporaryDirectory() as t, patch.object(sync, 'ROOT', Path(t)):
            root = Path(t); (root / 'data').mkdir(); (root / 'site').mkdir()
            payload = json.dumps(self.cached_data(ROWS))
            (root / 'data/collection.json').write_text(payload)
            (root / 'data/liked-songs.csv').write_text('existing-csv')
            (root / 'site/index.html').write_text('existing-html')
            api = FakeAPI([METADATA, {'recording-count': 2, 'recording-offset': 0, 'recordings': [{'id': r['id']} for r in ROWS]}, METADATA])
            status = sync.synchronize(api, CONFIG)
            self.assertFalse(status['content_changed']); self.assertFalse(status['metadata_refreshed'])
            self.assertEqual((status['metadata_fetched'], status['metadata_reused'], status['api_requests']), (0, 2, 3))
            self.assertTrue(all('inc' not in args[1] for args, _ in api.calls))
            self.assertEqual((root / 'data/collection.json').read_text(), payload)
            self.assertEqual((root / 'data/liked-songs.csv').read_text(), 'existing-csv')
            self.assertEqual((root / 'site/index.html').read_text(), 'existing-html')

    def test_incremental_add_delete_fetches_only_new_metadata(self):
        with tempfile.TemporaryDirectory() as t, patch.object(sync, 'ROOT', Path(t)):
            root = Path(t); (root / 'data').mkdir()
            old = self.cached_data([ROWS[0]])
            metadata = dict(METADATA, **{'recording-count': 1})
            old['collection_metadata'] = metadata
            (root / 'data/collection.json').write_text(json.dumps(old))
            api = FakeAPI([metadata, {'recording-count': 1, 'recording-offset': 0, 'recordings': [{'id': ROWS[1]['id']}]}, metadata, ROWS[1]])
            with patch.object(sync, 'write_snapshot') as write:
                status = sync.synchronize(api, CONFIG)
            self.assertEqual((status['added'], status['removed']), (1, 1))
            self.assertFalse(status['metadata_refreshed'])
            self.assertEqual(write.call_args[0][1], [ROWS[1]])
            self.assertEqual((status['metadata_fetched'], status['metadata_reused']), (1, 0))
            self.assertNotIn('inc', api.calls[1][0][1])
            self.assertEqual(api.calls[-1][0], ('/ws/2/recording/' + ROWS[1]['id'], {'inc': sync.METADATA_INCLUDES, 'fmt': 'json'}))

    def test_deletions_reuse_retained_metadata_without_lookups(self):
        with tempfile.TemporaryDirectory() as t, patch.object(sync, 'ROOT', Path(t)):
            root = Path(t); (root / 'data').mkdir()
            (root / 'data/collection.json').write_text(json.dumps(self.cached_data(ROWS)))
            metadata = dict(METADATA, **{'recording-count': 1})
            api = FakeAPI([metadata, {'recording-count': 1, 'recording-offset': 0, 'recordings': [{'id': ROWS[0]['id']}]}, metadata])
            with patch.object(sync, 'write_snapshot') as write:
                status = sync.synchronize(api, CONFIG)
            self.assertEqual((status['added'], status['removed'], status['metadata_fetched'], status['metadata_reused']), (0, 1, 0, 1))
            self.assertEqual(len(api.calls), 3)
            self.assertEqual(write.call_args[0][1], [ROWS[0]])

    def test_failed_new_recording_lookup_preserves_snapshot_and_exports(self):
        with tempfile.TemporaryDirectory() as t, patch.object(sync, 'ROOT', Path(t)):
            root = Path(t); (root / 'data').mkdir(); (root / 'site').mkdir()
            payload = json.dumps(self.cached_data([ROWS[0]]))
            existing = {'data/collection.json': payload, 'data/liked-songs.csv': 'existing-csv', 'site/index.html': 'existing-html', 'site/sync-status.json': 'existing-status'}
            for path, text in existing.items(): (root / path).write_text(text)
            api = FakeAPI([METADATA, {'recording-count': 2, 'recording-offset': 0, 'recordings': [{'id': r['id']} for r in ROWS]}, METADATA, {'id': 'merged-recording'}])
            with self.assertRaisesRegex(RuntimeError, 'merged'), patch.object(sync, 'write_snapshot') as write:
                sync.synchronize(api, CONFIG)
            write.assert_not_called()
            for path, text in existing.items(): self.assertEqual((root / path).read_text(), text)

    def test_two_month_refresh_advances_freshness_without_rebuilding_unchanged_exports(self):
        with tempfile.TemporaryDirectory() as t, patch.object(sync, 'ROOT', Path(t)):
            root = Path(t); (root / 'data').mkdir(); (root / 'site/csv').mkdir(parents=True)
            cached = self.cached_data(ROWS)
            cached['metadata_refreshed_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=70)).isoformat()
            (root / 'data/collection.json').write_text(json.dumps(cached))
            existing = {'data/liked-songs.csv': b'existing-csv', 'data/liked-songs.csv.gz': b'existing-gzip', 'site/index.html': b'existing-html', 'site/csv/f.json': b'existing-download'}
            for path, content in existing.items(): (root / path).write_bytes(content)
            api = FakeAPI([METADATA, {'recording-count': 2, 'recording-offset': 0, 'recordings': ROWS}, METADATA])
            status = sync.synchronize(api, CONFIG)
            self.assertTrue(status['metadata_refreshed']); self.assertFalse(status['content_changed'])
            self.assertEqual((status['metadata_fetched'], status['metadata_reused']), (2, 0))
            self.assertEqual(api.calls[1][0][1]['inc'], sync.METADATA_INCLUDES)
            self.assertGreater(sync.load_snapshot()['metadata_refreshed_at'], cached['metadata_refreshed_at'])
            for path, content in existing.items(): self.assertEqual((root / path).read_bytes(), content)
            # The advanced timestamp prevents another full refresh the next day.
            api = FakeAPI([METADATA, {'recording-count': 2, 'recording-offset': 0, 'recordings': [{'id': r['id']} for r in ROWS]}, METADATA])
            status = sync.synchronize(api, CONFIG)
            self.assertFalse(status['metadata_refreshed'])
            self.assertNotIn('inc', api.calls[1][0][1])

    def test_weekly_membership_check_reuses_metadata_younger_than_two_months(self):
        with tempfile.TemporaryDirectory() as t, patch.object(sync, 'ROOT', Path(t)):
            root = Path(t); (root / 'data').mkdir()
            cached = self.cached_data(ROWS)
            cached['metadata_refreshed_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=55)).isoformat()
            (root / 'data/collection.json').write_text(json.dumps(cached))
            api = FakeAPI([METADATA, {'recording-count': 2, 'recording-offset': 0, 'recordings': [{'id': r['id']} for r in ROWS]}, METADATA])
            status = sync.synchronize(api, CONFIG)
            self.assertFalse(status['metadata_refreshed'])
            self.assertEqual((status['metadata_fetched'], status['metadata_reused']), (0, 2))
            self.assertTrue(all('inc' not in args[1] for args, _ in api.calls))

    def test_refresh_uses_two_calendar_months_including_month_end_and_leap_year(self):
        for start, deadline in (
            ('2026-01-31T12:00:00+00:00', '2026-03-31T12:00:00+00:00'),
            ('2026-12-31T12:00:00+00:00', '2027-02-28T12:00:00+00:00'),
            ('2023-12-31T12:00:00+00:00', '2024-02-29T12:00:00+00:00'),
            ('2024-02-29T12:00:00+00:00', '2024-04-29T12:00:00+00:00'),
        ):
            refreshed_at = dt.datetime.fromisoformat(start)
            due_at = dt.datetime.fromisoformat(deadline)
            self.assertFalse(sync.metadata_refresh_due(refreshed_at, due_at - dt.timedelta(seconds=1)))
            self.assertTrue(sync.metadata_refresh_due(refreshed_at, due_at))

    def test_api_and_oauth_requests_include_identifying_user_agent(self):
        config = json.loads((Path(__file__).resolve().parents[1] / 'config.json').read_text())
        env = {'MUSICBRAINZ_CLIENT_ID': 'test-client', 'MUSICBRAINZ_CLIENT_SECRET': 'test-secret', 'MUSICBRAINZ_REFRESH_TOKEN': 'test-refresh'}
        responses = [io.BytesIO(json.dumps({'access_token': 'test-access', 'refresh_token': 'test-refresh', 'expires_in': 3600}).encode()), io.BytesIO(b'{"id":"recording"}')]
        with patch.dict(sync.os.environ, env), patch.object(sync.urllib.request, 'urlopen', side_effect=responses) as send, patch.object(sync.time, 'sleep'):
            api = sync.MusicBrainz(config)
            self.assertEqual(api.request('/ws/2/recording/recording')['id'], 'recording')
        requests = [call.args[0] for call in send.call_args_list]
        self.assertEqual(len(requests), 2)
        self.assertTrue(requests[0].full_url.endswith('/oauth2/token'))
        self.assertIsNone(requests[0].get_header('Authorization'))
        self.assertEqual(requests[1].get_header('Authorization'), 'Bearer test-access')
        for request in requests:
            self.assertEqual(request.get_header('User-agent'), config['user_agent'])
            self.assertIn('https://github.com/nabekhan/liked-songs', request.get_header('User-agent'))

    def test_socket_read_timeout_is_retried(self):
        env = {'MUSICBRAINZ_CLIENT_ID': 'test-client', 'MUSICBRAINZ_CLIENT_SECRET': 'test-secret', 'MUSICBRAINZ_REFRESH_TOKEN': 'test-refresh'}
        with patch.dict(sync.os.environ, env), patch.object(sync.urllib.request, 'urlopen', side_effect=[sync.socket.timeout('read timed out'), io.BytesIO(b'{"id":"recording"}')]) as send, patch.object(sync.time, 'sleep'):
            api = sync.MusicBrainz({'user_agent': 'TestSync/1.0 (https://example.org)'})
            self.assertEqual(api.request('/ws/2/recording/recording', authenticated=False)['id'], 'recording')
        self.assertEqual(send.call_count, 2)

    def test_random_request_spacing_respects_rate_limit(self):
        env = {'MUSICBRAINZ_CLIENT_ID': 'test-client', 'MUSICBRAINZ_CLIENT_SECRET': 'test-secret', 'MUSICBRAINZ_REFRESH_TOKEN': 'test-refresh'}
        with patch.dict(sync.os.environ, env), patch.object(sync.random, 'uniform', side_effect=[1.1, 3.0]), patch.object(sync.time, 'monotonic', return_value=100), patch.object(sync.time, 'sleep') as sleep, patch.object(sync.urllib.request, 'urlopen', side_effect=[io.BytesIO(b'{}'), io.BytesIO(b'{}')]):
            api = sync.MusicBrainz({'user_agent': 'TestSync/1.0 (https://example.org)'})
            api.last_request = 100
            api.request('/ws/2/recording/one', authenticated=False)
            api.request('/ws/2/recording/two', authenticated=False)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.1, 3.0])

    def test_explicit_refresh_overrides_recent_metadata(self):
        with tempfile.TemporaryDirectory() as t, patch.object(sync, 'ROOT', Path(t)):
            root = Path(t); (root / 'data').mkdir()
            (root / 'data/collection.json').write_text(json.dumps(self.cached_data(ROWS)))
            api = FakeAPI([METADATA, {'recording-count': 2, 'recording-offset': 0, 'recordings': ROWS}, METADATA])
            with patch.object(sync, 'write_snapshot'):
                status = sync.synchronize(api, CONFIG, refresh_metadata=True)
            self.assertTrue(status['metadata_refreshed'])
            self.assertEqual(api.calls[1][0][1]['inc'], sync.METADATA_INCLUDES)

    def test_first_sync_fetches_all_metadata(self):
        with tempfile.TemporaryDirectory() as t, patch.object(sync, 'ROOT', Path(t)):
            api = FakeAPI([METADATA, {'recording-count': 2, 'recording-offset': 0, 'recordings': ROWS}, METADATA])
            with patch.object(sync, 'write_snapshot') as write:
                status = sync.synchronize(api, CONFIG)
            self.assertTrue(status['metadata_refreshed'])
            self.assertEqual(status['added'], 2)
            self.assertEqual(api.calls[1][0][1]['inc'], sync.METADATA_INCLUDES)
            self.assertTrue(write.call_args.kwargs['rebuild_exports'])

    def test_complete_paginated_snapshot(self):
        api = FakeAPI([METADATA, {'recording-count': 2, 'recording-offset': 0, 'recordings': ROWS[:1]}, {'recording-count': 2, 'recording-offset': 1, 'recordings': ROWS[1:]}, METADATA])
        _, rows = sync.read_collection(api, CONFIG)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(api.calls), 4)
        for args, _ in api.calls[1:3]:
            self.assertEqual(args[0], '/ws/2/recording')
            self.assertEqual(args[1]['inc'], sync.METADATA_INCLUDES)
            self.assertEqual(args[1]['limit'], 100)

    def test_duplicate_page_is_rejected(self):
        api = FakeAPI([METADATA, {'recording-count': 2, 'recording-offset': 0, 'recordings': [ROWS[0], ROWS[0]]}, METADATA])
        with self.assertRaises(RuntimeError): sync.read_collection(api, CONFIG)

    def test_changed_collection_is_rejected(self):
        api = FakeAPI([METADATA, {'recording-count': 3, 'recording-offset': 0, 'recordings': ROWS}])
        with self.assertRaises(RuntimeError): sync.read_collection(api, CONFIG)

    def test_empty_page_is_rejected(self):
        api = FakeAPI([METADATA, {'recording-count': 2, 'recording-offset': 0, 'recordings': []}])
        with self.assertRaises(RuntimeError): sync.read_collection(api, CONFIG)

    def test_artist_credit_and_ids_are_preserved(self):
        row = sync.flatten_recording(ROWS[0])
        self.assertEqual(row['artist'], 'Tkandz & CXSPER')
        self.assertEqual(row['isrcs'], 'TEST123')
        self.assertEqual(row['recording_mbid'], ROWS[0]['id'])

    def test_exports_preserve_extra_fields_and_relationship_order(self):
        recording = {
            'id': ROWS[0]['id'], 'title': '=formula', 'first-release-date': '2000-01',
            'annotation': '@annotation', 'future-field': {'nested': ['one', 'two']},
            'artist-credit': [
                {'name': 'Alias', 'joinphrase': ' & ', 'artist': {'id': 'artist-1', 'name': 'Canonical', 'country': 'CA', 'aliases': [{'name': 'Alias'}]}},
                {'name': 'Second', 'artist': {'id': 'artist-2', 'name': 'Second'}},
            ],
            'relations': [
                {'type': 'performance', 'target-type': 'work', 'begin': '2000', 'attributes': ['live'], 'work': {'id': 'work-1', 'title': 'Work', 'iswcs': ['ISWC1'], 'languages': ['eng', 'fra']}},
                {'type': 'performance', 'target-type': 'work', 'begin': '2001', 'attributes': [], 'work': {'id': 'work-1', 'title': 'Work', 'iswcs': ['ISWC1'], 'languages': ['eng', 'fra']}},
                {'type': 'producer', 'target-type': 'artist', 'artist': {'id': 'producer-1', 'name': 'Producer'}},
            ],
        }
        with tempfile.TemporaryDirectory() as t, patch.object(sync, 'ROOT', Path(t)):
            root = Path(t); (root / 'scripts').mkdir()
            (root / 'scripts/page.html').write_text('<script type="application/json"><!-- COLLECTION_DATA --></script>')
            metadata = dict(METADATA, **{'recording-count': 2, 'type': 'Recording', 'type-id': 'collection-type'})
            sync.write_snapshot(metadata, [recording, ROWS[1]], CONFIG)
            with (root / 'data/liked-songs.csv').open(encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f); fields = reader.fieldnames; rows = list(reader)
            row = next(row for row in rows if row['recording_mbid'] == recording['id'])
            self.assertEqual(fields[:11], sync.CSV_BASE_FIELDS)
            self.assertEqual(row['first_release_date'], '2000-01')
            self.assertEqual(json.loads(row['artist_country']), ['CA', None])
            self.assertEqual(json.loads(row['artist_name']), ['Canonical', 'Second'])
            self.assertEqual(json.loads(row['artist_aliases']), [[{'name': 'Alias'}], None])
            self.assertEqual(row['work_mbids'], 'work-1')
            self.assertEqual(json.loads(row['relationship_work_mbids']), ['work-1', 'work-1', None])
            self.assertEqual(json.loads(row['relationship_begin']), ['2000', '2001', None])
            self.assertEqual(json.loads(row['relationship_attributes']), [['live'], [], None])
            self.assertEqual(json.loads(row['relationship_artist']), [None, None, recording['relations'][2]['artist']])
            self.assertEqual(json.loads(row['work_iswcs']), [['ISWC1'], ['ISWC1'], None])
            self.assertEqual(json.loads(row['future_field']), recording['future-field'])
            self.assertEqual(row['collection_type_id'], 'collection-type')
            self.assertEqual(row['title'], "'=formula")
            self.assertEqual(row['annotation'], "'@annotation")
            self.assertEqual(row['video'], 'false')
            self.assertNotIn('id', fields); self.assertNotIn('length', fields)
            html = (root / 'site/index.html').read_text()
            page = json.loads(html.split('>', 1)[1].rsplit('</script>', 1)[0])
            self.assertEqual(page['csv_fields'], fields)
            self.assertEqual(set(page['songs'][0]), set(sync.CSV_BASE_FIELDS) | {'first_release_date', 'languages'})
            song = next(song for song in page['songs'] if song['recording_mbid'] == recording['id'])
            self.assertEqual(song['first_release_date'], '2000-01')
            self.assertEqual(song['languages'], ['eng', 'fra'])
            chunk = json.loads((root / 'site/csv/f.json').read_text())[0]
            self.assertEqual(json.loads(chunk['future_field']), recording['future-field'])
            self.assertEqual(json.loads((root / 'data/collection.json').read_text())['collection_metadata'], metadata)
            with gzip.open(root / 'data/liked-songs.csv.gz', 'rb') as compressed:
                self.assertEqual(compressed.read(), (root / 'data/liked-songs.csv').read_bytes())
            sync.build_page()
            rebuilt = (root / 'site/index.html').read_text()
            rebuilt = json.loads(rebuilt.split('>', 1)[1].rsplit('</script>', 1)[0])
            self.assertEqual(rebuilt['csv_fields'], fields)
            with gzip.open(root / 'data/collection.json.gz', 'rt', encoding='utf-8') as compressed:
                self.assertEqual(json.load(compressed)['recordings'], [recording, ROWS[1]])
            (root / 'data/collection.json').unlink()
            self.assertEqual(sync.load_snapshot()['recordings'], [recording, ROWS[1]])
            sync.materialize_compressed_exports()
            self.assertEqual(json.loads((root / 'data/collection.json').read_text())['recordings'], [recording, ROWS[1]])
            with gzip.open(root / 'data/liked-songs.csv.gz', 'rb') as compressed:
                self.assertEqual(compressed.read(), (root / 'data/liked-songs.csv').read_bytes())

    def test_changed_metadata_request_forces_refresh_and_updates_snapshot(self):
        with tempfile.TemporaryDirectory() as t, patch.object(sync, 'ROOT', Path(t)):
            root = Path(t); (root / 'data').mkdir()
            cached = self.cached_data(ROWS); cached['metadata_includes'] = 'artist-credits+isrcs+work-rels'
            (root / 'data/collection.json').write_text(json.dumps(cached))
            with patch.object(sync, 'read_collection', return_value=(METADATA, ROWS)) as read, patch.object(sync, 'write_snapshot') as write:
                status = sync.synchronize(FakeAPI([]), CONFIG)
            self.assertTrue(status['metadata_refreshed']); self.assertTrue(status['content_changed'])
            self.assertTrue(read.call_args.kwargs['full_metadata'])
            write.assert_called_once()

    def test_page_embedded_json_cannot_inject_script(self):
        with tempfile.TemporaryDirectory() as t, patch.object(sync, 'ROOT', Path(t)):
            (Path(t) / 'scripts').mkdir()
            (Path(t) / 'scripts/page.html').write_text('<script type="application/json"><!-- COLLECTION_DATA --></script>')
            data = {'collection_id': CID, 'collection_name': 'Liked Songs', 'collection_owner': 'nabecite', 'synced_at': '2026-10-12T00:00:00Z', 'count': 2, 'recordings': ROWS}
            sync.build_page(data)
            html = (Path(t) / 'site/index.html').read_text()
            self.assertNotIn('<script>alert', html)
            payload = json.loads(html.split('>', 1)[1].rsplit('</script>', 1)[0])
            self.assertEqual(payload['songs'][0]['title'], ROWS[1]['title'])

if __name__ == '__main__': unittest.main()
