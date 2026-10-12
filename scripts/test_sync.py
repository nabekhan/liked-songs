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
    def __init__(self, responses): self.responses = iter(responses)
    def request(self, *args, **kwargs): return next(self.responses)

class SyncTests(unittest.TestCase):
    def cached_data(self, rows):
        return {'collection_id': CID, 'collection_owner': 'nabecite', 'collection_name': 'Liked Songs', 'synced_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'metadata_refreshed_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'recordings': sorted(rows, key=lambda r: r['id']), 'count': len(rows)}

    def test_no_change_leaves_large_files_untouched(self):
        with tempfile.TemporaryDirectory() as t, patch.object(sync, 'ROOT', Path(t)):
            root = Path(t); (root / 'data').mkdir(); (root / 'site').mkdir()
            payload = json.dumps(self.cached_data(ROWS))
            (root / 'data/collection.json').write_text(payload)
            (root / 'data/liked-songs.csv').write_text('existing-csv')
            (root / 'site/index.html').write_text('existing-html')
            with patch.object(sync, 'read_collection', return_value=(METADATA, ROWS)):
                status = sync.synchronize(FakeAPI([]), CONFIG)
            self.assertFalse(status['content_changed']); self.assertFalse(status['metadata_refreshed'])
            self.assertEqual((root / 'data/collection.json').read_text(), payload)
            self.assertEqual((root / 'data/liked-songs.csv').read_text(), 'existing-csv')
            self.assertEqual((root / 'site/index.html').read_text(), 'existing-html')

    def test_incremental_add_delete_fetches_only_new_metadata(self):
        with tempfile.TemporaryDirectory() as t, patch.object(sync, 'ROOT', Path(t)):
            root = Path(t); (root / 'data').mkdir()
            old = self.cached_data([ROWS[0]])
            (root / 'data/collection.json').write_text(json.dumps(old))
            with patch.object(sync, 'read_collection', return_value=(METADATA, [ROWS[1]])), patch.object(sync, 'write_snapshot') as write:
                status = sync.synchronize(FakeAPI([ROWS[1]]), CONFIG)
            self.assertEqual((status['added'], status['removed']), (1, 1))
            self.assertFalse(status['metadata_refreshed'])
            self.assertEqual(write.call_args[0][1], [ROWS[1]])

    def test_complete_paginated_snapshot(self):
        api = FakeAPI([METADATA, {'recording-count': 2, 'recording-offset': 0, 'recordings': ROWS[:1]}, {'recording-count': 2, 'recording-offset': 1, 'recordings': ROWS[1:]}, METADATA])
        _, rows = sync.read_collection(api, CONFIG)
        self.assertEqual(len(rows), 2)

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
