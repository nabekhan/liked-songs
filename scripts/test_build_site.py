import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import build_site


class BuildSiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        (self.root / "site").mkdir()
        (self.root / "scripts/page.html").write_text(
            '<script id="collection-data" type="application/json"><!-- COLLECTION_DATA --></script>'
        )
        song = {key: '' for key in build_site.SONG_TEXT_FIELDS}
        song.update(video=False, length_ms=None, languages=[])
        self.payload = {"count": 2, "songs": [dict(song, recording_mbid='one'), dict(song, recording_mbid='two')],
                        'collection_id': 'collection', 'collection_name': 'Songs', 'collection_owner': 'owner',
                        'synced_at': '2026-10-16T00:00:00+00:00', 'csv_fields': build_site.sync.CSV_BASE_FIELDS}
        encoded = json.dumps(self.payload, separators=(",", ":")).replace("<", "\\u003c")
        (self.root / "site/index.html").write_text(
            f'<script id="collection-data" type="application/json">{encoded}</script>'
        )
        self.root_patch = patch.object(build_site, "ROOT", self.root)
        self.sync_root_patch = patch.object(build_site.sync, "ROOT", self.root)
        self.root_patch.start()
        self.sync_root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.sync_root_patch.stop()
        self.temp.cleanup()

    def test_template_only_reuses_exact_payload_without_export(self):
        original = build_site.collection_payload()
        (self.root / "scripts/page.html").write_text(
            '<main>new</main><script id="collection-data" type="application/json"><!-- COLLECTION_DATA --></script>'
        )
        with patch.object(build_site, "changed_paths", return_value={"scripts/page.html"}), patch.object(
            build_site.sync, "build_exports"
        ) as full:
            self.assertEqual(build_site.build("a" * 40), "template-only")
        full.assert_not_called()
        self.assertIn(original, (self.root / "site/index.html").read_text())

    def test_mixed_backend_or_data_changes_use_full_export(self):
        for paths in ({"scripts/sync.py"}, {"data/collection.json.gz"}, {"scripts/page.html", "config.json"}):
            with self.subTest(paths=paths), patch.object(build_site, "changed_paths", return_value=paths), patch.object(
                build_site.sync, "build_exports"
            ) as full:
                self.assertEqual(build_site.build("a" * 40), "full")
            full.assert_called_once_with()

    def test_bad_existing_payload_falls_back_to_full_export(self):
        (self.root / "site/index.html").write_text(
            '<script id="collection-data" type="application/json">{"count":2,"songs":[{"recording_mbid":"one"}]}</script>'
        )
        with patch.object(build_site, "changed_paths", return_value={"scripts/page.html"}), patch.object(
            build_site.sync, "build_exports"
        ) as full:
            self.assertEqual(build_site.build("a" * 40), "full")
        full.assert_called_once_with()

    def test_unknown_base_uses_full_export(self):
        with patch.object(build_site.sync, "build_exports") as full:
            self.assertEqual(build_site.build(None), "full")
        full.assert_called_once_with()

    def test_site_changed_detects_committed_generated_site_change(self):
        with patch.object(build_site, "git") as git:
            git.side_effect = [type("R", (), {"returncode": 1})()]
            self.assertTrue(build_site.site_changed("a" * 40))

    def test_site_changed_skips_when_site_matches_and_no_untracked_files(self):
        with patch.object(build_site, "git") as git:
            git.side_effect = [
                type("R", (), {"returncode": 0})(),
                SimpleNamespace(returncode=0, stdout=""),
            ]
            self.assertFalse(build_site.site_changed("a" * 40))

    def test_unknown_base_publishes_conservatively(self):
        self.assertTrue(build_site.site_changed(None))

    def test_malformed_payload_falls_back(self):
        for payload in ({'count': 1, 'songs': None}, {'count': 1, 'songs': [{}]},
                        {'count': 1, 'songs': [{'recording_mbid': []}]},
                        {'count': 2, 'songs': [{'recording_mbid': 'same'}] * 2}):
            with self.subTest(payload=payload):
                (self.root / 'site/index.html').write_text(
                    '<script id="collection-data" type="application/json">' + json.dumps(payload) + '</script>')
                self.assertIsNone(build_site.collection_payload())

    def test_incomplete_display_or_csv_payload_falls_back(self):
        for kind in ('missing title', 'invalid languages', 'missing csv fields'):
            payload = json.loads(json.dumps(self.payload))
            if kind == 'missing title': del payload['songs'][0]['title']
            elif kind == 'invalid languages': payload['songs'][0]['languages'] = 'eng'
            else: del payload['csv_fields']
            with self.subTest(kind=kind):
                (self.root / 'site/index.html').write_text(
                    '<script id="collection-data" type="application/json">' + json.dumps(payload) + '</script>')
                with patch.object(build_site, 'changed_paths', return_value={'scripts/page.html'}), patch.object(build_site.sync, 'build_exports') as full:
                    self.assertEqual(build_site.build('a' * 40), 'full')
                full.assert_called_once_with()

    def test_real_git_compares_committed_and_uncommitted_site_changes(self):
        build_site.git('init', check=True)
        build_site.git('config', 'user.name', 'Test', check=True)
        build_site.git('config', 'user.email', 'test@example.org', check=True)
        build_site.git('add', '.', check=True)
        build_site.git('commit', '-m', 'baseline', check=True)
        base = build_site.git('rev-parse', 'HEAD', check=True).stdout.strip()
        self.assertFalse(build_site.site_changed(base))
        (self.root / 'scripts/page.html').write_text('new<!-- COLLECTION_DATA -->')
        build_site.git('add', '.', check=True)
        build_site.git('commit', '-m', 'template', check=True)
        self.assertEqual(build_site.changed_paths(base), {'scripts/page.html'})
        with patch.object(build_site.sync, 'build_exports') as full:
            self.assertEqual(build_site.build(base), 'template-only')
        full.assert_not_called()
        self.assertTrue(build_site.site_changed(base))
        build_site.git('add', '.', check=True)
        build_site.git('commit', '-m', 'generated page', check=True)
        self.assertTrue(build_site.site_changed(base))
        current = build_site.git('rev-parse', 'HEAD', check=True).stdout.strip()
        self.assertFalse(build_site.site_changed(current))
        (self.root / 'site/new.json').write_text('{}')
        self.assertTrue(build_site.site_changed(current))

    def test_base_resolution_rejects_invalid_sha_without_fetch(self):
        with patch.object(build_site, 'git') as git:
            for sha in ('0' * 40, '--unsafe', '', None):
                self.assertIsNone(build_site.resolve_base(sha))
        git.assert_not_called()

