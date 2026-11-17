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

## MusicBrainz → ListenBrainz loves (disabled until enabled)

`Sync ListenBrainz Loves` is a separate, opt-in GitHub Actions workflow. It has
no Pages deployment steps and does not modify the website or its snapshots.
While `LISTENBRAINZ_SYNC_ENABLED` is unset, scheduled runs and manual apply runs
are skipped. Merging the code does not enable ListenBrainz writes.

### What it does

1. Every hour at minute 17, read the **live** recording collection in `config.json`
   using the existing MusicBrainz OAuth credentials. Download membership only,
   without expanded recording metadata or artwork. Verify collection identity,
   page offsets, counts, and unique recording MBIDs before planning any changes.
2. Read the user's current ListenBrainz feedback, in pages of 1,000 without
   metadata. Compare recording MBIDs directly; no title/artist guessing occurs.
3. Love collection members that are not already loved (`score: 1`). First-time
   setup includes the entire collection, not just additions made after setup.
4. Clear loves for recordings previously managed by this sync that have left
   the collection (`score: 0`). Unrelated ListenBrainz loves and explicit hates
   on removed entries are preserved. Existing collection members are always
   loved, even if manually unliked or hated in ListenBrainz between runs.
5. Save ownership/progress to `state.json` on the dedicated
   `listenbrainz-sync-state` branch. This is separate from the weekly site's
   sync and does not push `main`. Intended ownership is committed **before**
   feedback writes, and progress again afterward, including on API failures.

The MusicBrainz owner is also the default ListenBrainz username. Set
`listenbrainz_user` in `config.json` only when the target username differs.
The token is validated against that username before any feedback changes.

### Preview and eventual activation

**Do not enable this while a separate ListenBrainz cleanup/reset is running.**
Wait for that process to finish and review the proposed behavior first.

The default command is read-only and does not save ownership state:

```sh
python3 scripts/sync_listenbrainz.py
```

This uses the existing local MusicBrainz authorization (or its three OAuth
environment variables) and public ListenBrainz feedback. No ListenBrainz token
is required for previews. A preview reports planned additions/removals without
listing thousands of recording IDs. To preview against existing ownership,
retrieve `state.json` from `listenbrainz-sync-state` and pass `--state PATH`.

After cleanup and review, future activation requires:

1. Add repository Actions secret `LISTENBRAINZ_TOKEN` for the configured user.
   Reuse the three existing `MUSICBRAINZ_*` secrets; nothing is hard-coded.
2. Optionally run **Actions → Sync ListenBrainz Loves → Run workflow** with
   **apply** unchecked for a read-only preview against the durable checkpoint.
3. Set repository Actions variable `LISTENBRAINZ_SYNC_ENABLED` to `true`.
   Scheduled runs then apply changes; a manual run writes only if **apply** is
   checked. Unset the variable to pause future writes.

Local writes, when deliberately requested, use `LISTENBRAINZ_TOKEN` in the
environment plus `--apply`. Preserve the local state file between runs. The
`--publish-state` flag is for the GitHub workflow: it checkpoints to the remote
state branch, using the checkout's existing Git credentials. Credentials never
go into state files or committed configuration.

### Limits and recovery

- **Polling, not instant events.** Changes normally appear on the next hourly
  run; GitHub scheduling can be delayed. Reading this approximately 15,000-song
  collection takes around three minutes at MusicBrainz's one-request-per-second
  limit. The weekly website sync is independent and may display an older list.
- **Initial backfill is slow.** ListenBrainz accepts one feedback item per write.
  Each run applies up to 5,000 changes within a 30-minute ListenBrainz budget,
  with retry/backoff and server rate-limit headers. Large backfills span several
  hourly runs. The job has a 45-minute overall timeout; later runs reconcile
  actual target feedback and resume. A final in-flight request may exceed the
  30-minute budget by its 30-second network timeout.
- **Collection membership is authoritative for managed loves.** Removing and
  later re-adding a recording clears and restores its love. This is one-way;
  ListenBrainz edits do not update MusicBrainz. MSID-only feedback cannot be
  reliably matched to collection recording MBIDs and is left untouched.
- **APIs do not provide a transactional snapshot or conditional writes.** Count
  changes, duplicate pages, incomplete responses, and conflicting feedback are
  rejected before mutation. A same-size collection edit during pagination can
  still evade those checks, and concurrent ListenBrainz edits can race a write.
  The next successful run converges to the current collection. Failed requests
  can have succeeded server-side; retries are safe because scores are idempotent.
- **Keep the state branch.** Deleting it loses knowledge of earlier managed
  removals. Existing collection loves can be adopted again, but old loves outside
  the collection can no longer be distinguished from unrelated manual loves.
  State-branch protection, lost Actions write permissions, OAuth expiry, token
  changes, or API outages stop progress rather than bypassing validation. A
  checkpoint push failure before a run's first write prevents that run's writes.

Read-only tests, including a temporary local Git remote for checkpoint tests:

```sh
python3 -m unittest discover -s scripts -p 'test_listenbrainz.py'
```

API references: [MusicBrainz collections and rate limits](https://musicbrainz.org/doc/MusicBrainz_API),
[ListenBrainz feedback API](https://listenbrainz.readthedocs.io/en/latest/users/api/recordings.html),
and [feedback score meanings](https://listenbrainz.readthedocs.io/en/latest/users/feedback-json.html).
