# Liked Songs

A searchable website mirroring the MusicBrainz recording collection in
`config.json`.

## GitHub Actions

`Sync Liked Songs` runs once a week, Sundays at 03:43 UTC. GitHub can delay
scheduled runs. Use **Actions → Sync Liked Songs → Run workflow** for an
extra sync; select **refresh_metadata** to refresh every recording's metadata.

Scheduled and manual runs sync the MusicBrainz collection, build the website,
commit changed snapshots, and deploy to GitHub Pages when the website changes.
Pushes to `main` that change scripts, configuration, workflow, or data rebuild
and deploy the website without contacting MusicBrainz.

## Files to maintain

- `scripts/sync.py`: MusicBrainz sync, metadata cache, and export generation.
- `scripts/build_site.py`: website builds and checks for Pages changes.
- `scripts/page.html`: searchable website template.
- `scripts/authorize.py`: MusicBrainz credential setup and recovery.
- `scripts/test_*.py` and `scripts/test_table_window.mjs`: sync, build, and
  browser-table regression tests.
- `data/collection.json.gz`: cached MusicBrainz collection and metadata.
- `data/liked-songs.csv.gz`: compressed CSV export.
- `data/sync-status.json`: last sync status.
- `site/`: generated GitHub Pages website and downloadable exports.

The workflow requires the repository secrets `MUSICBRAINZ_CLIENT_ID`,
`MUSICBRAINZ_CLIENT_SECRET`, and `MUSICBRAINZ_REFRESH_TOKEN`. To set up or renew
them, run `python3 scripts/authorize.py --github` locally with the GitHub CLI
authenticated. Never commit credentials.

## Local checks

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
node --test scripts/test_table_window.mjs
```

To rebuild the website from the cached collection without contacting
MusicBrainz, run `python3 scripts/build_site.py --build`.
