# Liked Songs

A searchable mirror of [nabecite’s MusicBrainz Liked Songs recording collection](https://musicbrainz.org/collection/4cad0217-1cbe-43ae-b148-afa5afad7625).

**[Open the searchable collection](https://nabekhan.github.io/musicbrainz-liked-songs/)**

Search song titles, performing artists, ISRCs, recording MBIDs, artist MBIDs, or linked works. All words must match; quotation marks keep a phrase together. Each result opens the exact MusicBrainz recording in the collection. Download the current search results as CSV, or use [`data/liked-songs.csv`](data/liked-songs.csv) for the complete list.

## Automatic updates

GitHub Actions checks the MusicBrainz collection daily at 12:23 UTC. Scheduled runs can be delayed by GitHub. To update sooner, open **Actions → Sync Liked Songs → Run workflow**. The workflow records additions and removals, commits the verified snapshot, and deploys GitHub Pages. See [`data/sync-status.json`](data/sync-status.json) for the last successful check.

MusicBrainz does not expose a collection membership change feed. Every run therefore scans the current list of recording IDs, respecting the API’s one-request-per-second limit. After the first sync:

- Existing recording metadata is reused.
- Metadata is fetched only for added recordings; removed recordings leave the mirror.
- If nothing changed, the large JSON snapshot, CSV, and HTML are left unchanged. Only the small check-status file is updated.
- Every seven days, the full metadata is refreshed to capture MusicBrainz edits and new work/ISRC relationships.

A complete paginated response is required before any snapshot is replaced. Authentication failures, server errors, count changes during pagination, overlapping pages, and incomplete pages fail the sync and retain the previous published list. Retries respect server backoff. Concurrent workflow runs are serialized.

This repository mirrors **MusicBrainz only**. It never changes the MusicBrainz collection, recording metadata, or ListenBrainz loves. Collection membership remains recording-specific; a selected recording can serve as your representative for a song, but other versions are not automatically members.

## Data and privacy

The website and repository are public, including the song list and its metadata. The original MusicBrainz collection can remain private. OAuth credentials are stored in GitHub Actions secrets and are never committed or embedded in the website. MusicBrainz’s `collection` OAuth scope permits reading and modifying private collections; this application uses it only to read the configured collection. It requests no profile, email, rating, tag, or database-edit permissions.

The local credentials are in `~/.config/musicbrainz-liked-songs/auth.json` with file mode `0600`, outside this repository. Revoke the application **Liked Songs GitHub Sync** on [MusicBrainz’s Applications page](https://musicbrainz.org/account/applications) to disable access.

## Local use

Python 3.9+ and Git are sufficient; no third-party Python packages are required.

```sh
git clone https://github.com/nabekhan/musicbrainz-liked-songs.git
cd musicbrainz-liked-songs
open site/index.html                  # macOS: searchable offline snapshot
python3 scripts/sync.py               # requires OAuth credentials
python3 scripts/sync.py --refresh-metadata
python3 scripts/sync.py --build-only  # rebuild from saved data, no network
python3 -m unittest discover -s scripts -p 'test_*.py'
```

For a fresh installation, run `python3 scripts/authorize.py` and follow its MusicBrainz application-registration instructions. Add `--github` to update the three repository secrets using an authenticated `gh` CLI. You can alternatively supply all three environment variables: `MUSICBRAINZ_CLIENT_ID`, `MUSICBRAINZ_CLIENT_SECRET`, and `MUSICBRAINZ_REFRESH_TOKEN`.

Configuration lives in [`config.json`](config.json). The source snapshot stores MusicBrainz recording metadata, including artist credits, duration, disambiguation, video status, ISRCs, and linked works. Metadata completeness depends on MusicBrainz.

For a local copy that updates itself from GitHub, run `python3 scripts/install_local_refresh.py`. It installs a user LaunchAgent that fast-forwards this checkout every six hours while your Mac is available. It refuses to update a dirty checkout and does not require an administrator password. The public website is the simplest option for browsing from any device.
