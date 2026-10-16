#!/usr/bin/env python3
"""Reconcile Spotify Liked Songs with a verified MusicBrainz snapshot.

Credentials and run data default to private directories outside this repository.
The backup and plan commands are read-only. Apply requires an unchanged source,
the same Spotify account, and a complete library backup.
"""
import argparse
from collections import Counter, defaultdict
import csv
import datetime as dt
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
AUTH = Path.home() / '.config/liked-songs-spotify/auth.json'
RUNS = Path.home() / '.local/share/liked-songs-spotify'
TRACK_ID = re.compile(r'^[A-Za-z0-9]{22}$')


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
    if path.suffix == '.gz':
        raw = gzip.compress(raw, mtime=0)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.sync-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == '.gz' else raw)


class SpotifyError(RuntimeError):
    def __init__(self, status, endpoint, retry_after=None):
        self.status, self.retry_after = status, retry_after
        super().__init__(f'Spotify returned HTTP {status} for {endpoint.split("?")[0]}')


class SpotifyCooldown(SpotifyError):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__(429, 'API cooldown', max(1, int(retry_at - time.time())))
        when = dt.datetime.fromtimestamp(retry_at, dt.timezone.utc).isoformat()
        self.args = (f'Spotify requires a cooldown until {when}; cached progress is preserved.',)


class Spotify:
    def __init__(self, auth_path=AUTH, interval=2.1):
        self.auth_path, self.interval, self.last = auth_path, interval, 0
        self.calls = 0
        self.connection = http.client.HTTPSConnection('api.spotify.com', timeout=45)
        if os.environ.get('SPOTIFY_REFRESH_TOKEN'):
            self.auth = {'client_id': os.environ['SPOTIFY_CLIENT_ID'],
                         'refresh_token': os.environ['SPOTIFY_REFRESH_TOKEN'],
                         'user_id': os.environ['SPOTIFY_USER_ID']}
            self.auth_path = None
        else:
            self.auth = read_json(auth_path)
        self.cooldown_path = auth_path.parent / 'rate-limit.json'
        self.client_key = hashlib.sha256(self.auth['client_id'].encode()).hexdigest()

    def check_cooldown(self):
        if self.cooldown_path.exists():
            limits = read_json(self.cooldown_path)
            retry_at = limits.get(self.client_key, 0)
            if time.time() < retry_at:
                raise SpotifyCooldown(retry_at)

    def set_cooldown(self, seconds):
        limits = read_json(self.cooldown_path) if self.cooldown_path.exists() else {}
        retry_at = time.time() + max(1, seconds) + 5
        limits[self.client_key] = max(limits.get(self.client_key, 0), retry_at)
        save_json(self.cooldown_path, limits)
        return limits[self.client_key]

    def refresh(self):
        body = urllib.parse.urlencode({'grant_type': 'refresh_token',
            'refresh_token': self.auth['refresh_token'], 'client_id': self.auth['client_id']}).encode()
        req = urllib.request.Request('https://accounts.spotify.com/api/token', data=body,
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                updated = json.load(response)
        except urllib.error.HTTPError as error:
            raise SpotifyError(error.code, 'OAuth token refresh') from None
        self.auth.update(updated)
        self.auth['expires_at'] = time.time() + updated['expires_in']
        if self.auth_path:
            save_json(self.auth_path, self.auth)

    def request(self, endpoint, params=None, method='GET', missing_ok=False):
        self.check_cooldown()
        if time.time() >= self.auth.get('expires_at', 0) - 60:
            self.refresh()
        url = 'https://api.spotify.com/v1/' + endpoint.lstrip('/')
        if params:
            url += '?' + urllib.parse.urlencode(params)
        for attempt in range(6):
            time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            self.calls += 1
            try:
                self.connection.request(method, url.removeprefix('https://api.spotify.com'), headers={
                    'Authorization': 'Bearer ' + self.auth['access_token'], 'Accept': 'application/json',
                    'Cache-Control': 'no-cache'})
                response = self.connection.getresponse()
                raw = response.read()
                status = response.status
                if status < 400:
                    return json.loads(raw) if raw else None
                if status == 404 and missing_ok:
                    return None
                if status == 401 and attempt == 0:
                    self.refresh()
                    continue
                if status == 429:
                    delay = max(1, int(response.getheader('Retry-After', '30')))
                    raise SpotifyCooldown(self.set_cooldown(delay))
                if status in (500, 502, 503, 504):
                    time.sleep(min(30, 2 ** attempt))
                    continue
                raise SpotifyError(status, endpoint)
            except (http.client.HTTPException, OSError, TimeoutError):
                self.connection.close()
                time.sleep(min(30, 2 ** attempt))
        raise RuntimeError('Spotify request failed after retries; resume the same run later.')

    def profile(self):
        profile = self.request('me')
        if self.auth.get('user_id') != profile['id']:
            raise RuntimeError('Spotify account differs from the authorized target.')
        return {'id': profile['id'], 'display_name': profile.get('display_name')}


def saved_id(item):
    track = item.get('track') or {}
    return (track.get('linked_from') or {}).get('id') or track.get('id')


def aliases(item):
    track = item.get('track') or {}
    return {value for value in (track.get('id'), saved_id(item)) if value}


def library(api):
    profile = api.profile()
    items, total, offset = [], None, 0
    while total is None or offset < total:
        page = api.request('me/tracks', {'limit': 50, 'offset': offset})
        if total is None:
            total = page['total']
        if page['total'] != total:
            raise RuntimeError('Spotify library changed during pagination; retry the backup.')
        rows = page['items']
        if len(rows) != min(50, total - offset):
            raise RuntimeError('Incomplete Spotify library page; previous backups preserved.')
        items.extend(rows)
        offset += len(rows)
        if offset % 500 == 0 or offset == total:
            print(f'Backed up Spotify tracks: {offset:,}/{total:,}', flush=True)
    ids = [saved_id(item) for item in items]
    if any(not value or not TRACK_ID.fullmatch(value) for value in ids):
        raise RuntimeError('Library contains local or unidentified tracks that need separate handling.')
    if len(set(ids)) != len(ids):
        raise RuntimeError('Overlapping Spotify library pages; retry the backup.')
    return {'account': profile, 'retrieved_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'total': total, 'items': items}


def normalize(value):
    value = unicodedata.normalize('NFKD', value).casefold().replace('&', ' and ')
    result = ''.join(c for c in value if c.isalnum())
    return result or value.strip()


EDITION = re.compile(r'\b(?:remaster(?:ed)?|live|remix|mix|edit|version|acoustic|instrumental|demo|'
                     r'mono|stereo|radio|single|album|deluxe|anniversary|bonus|explicit|clean|'
                     r'feat(?:uring)?|ft|music video|official video|outtake|chosen by)\b', re.I)


def base_title(value):
    def bracket(match):
        return '' if EDITION.search(match[0]) else match[0]
    value = re.sub(r'\([^)]*\)|\[[^]]*\]', bracket, value)
    value = re.sub(r'\s+(?:feat\.?|featuring|ft\.?)\s+.+$', '', value, flags=re.I)
    parts = re.split(r'\s+[\-–—]\s+', value)
    if len(parts) > 1 and EDITION.search(parts[-1]):
        value = ' - '.join(parts[:-1])
    return re.sub(r'\s+', ' ', value).strip()


def title_forms(value):
    """Alternative subtitles require independent ISRC and duration agreement."""
    forms = {normalize(value), normalize(base_title(value))}
    forms.update(normalize(part) for groups in re.findall(r'\(([^)]*)\)|\[([^]]*)\]', value)
                 for part in groups if part)
    forms.add(normalize(re.sub(r'\([^)]*\)|\[[^]]*\]', '', value)))
    forms.add(normalize(re.split(r'\s+[\-–—]\s+', value)[0]))
    forms.add(normalize(value.rsplit(':', 1)[-1]))
    return {form for form in forms if len(form) >= 6}


def supported_title_match(song, track):
    if not shared_isrc(song, track) or not duration_close(song, track):
        return False
    left, right = title_forms(song['title']), title_forms(track.get('name', ''))
    return bool(left & right)


def version_kind(title, disambiguation=''):
    hints = disambiguation + ' ' + ' '.join(re.findall(r'\([^)]*\)|\[[^]]*\]', title))
    parts = re.split(r'\s+[\-–—]\s+', title)
    if len(parts) > 1:
        hints += ' ' + parts[-1]
    for kind in ('live', 'remix', 'instrumental', 'acoustic', 'demo', 'karaoke'):
        if re.search(r'\b' + kind + r'\b', hints, re.I):
            return kind
    return 'studio'


def source_songs(document):
    songs = []
    for recording in document['recordings']:
        credits = recording.get('artist-credit', [])
        artists, artist_keys, lead_keys = [], set(), set()
        composers = set()
        for relation in recording.get('relations', []):
            work = relation.get('work', {})
            for rel in work.get('relations', []):
                if rel.get('type') == 'composer':
                    composers.add(rel.get('artist', {}).get('id'))
        noncomposers = [c for c in credits if c.get('artist', {}).get('id') not in composers]
        primary = noncomposers[:1] or credits[:1]
        for credit in credits:
            artist = credit.get('artist', {})
            names = {n for n in (credit.get('name'), artist.get('name')) if n}
            names.update(a['name'] for a in artist.get('aliases', []) if a.get('name'))
            keys = {normalize(name) for name in names}
            artist_keys.update(keys)
            if credit in primary:
                lead_keys.update(keys)
            if artist.get('name'):
                artists.append(artist['name'])
        titles = {recording['title']}
        titles.update(a['name'] for a in recording.get('aliases', []) if a.get('name'))
        spotify_ids = []
        for relation in recording.get('relations', []):
            if relation.get('target-type') == 'url':
                value = relation.get('url', {}).get('resource', '')
                spotify_ids.extend(re.findall(r'spotify\.com/(?:intl-[^/]+/)?track/([A-Za-z0-9]{22})', value))
        songs.append({'mbid': recording['id'], 'title': recording['title'],
            'artist': ' / '.join(artists), 'artist_names': artists,
            'artist_keys': sorted(artist_keys), 'lead_keys': sorted(lead_keys),
            'title_keys': sorted({normalize(t) for t in titles}),
            'base_keys': sorted({normalize(base_title(t)) for t in titles}),
            'query_title': base_title(recording['title']),
            'version': version_kind(recording['title'], recording.get('disambiguation', '')),
            'length_ms': recording.get('length'), 'isrcs': recording.get('isrcs', []),
            'spotify_ids': list(dict.fromkeys(spotify_ids))})
    if len(songs) != document['count'] or len({s['mbid'] for s in songs}) != len(songs):
        raise RuntimeError('MusicBrainz snapshot count or recording identifiers are inconsistent.')
    return songs


def load_source(path):
    if path.exists():
        raw = path.read_bytes()
    else:
        raw = subprocess.check_output(['git', 'show', 'HEAD:data/collection.json.gz'], cwd=ROOT)
    document = json.loads(gzip.decompress(raw) if path.suffix == '.gz' else raw)
    songs = source_songs(document)
    oid = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    current = subprocess.check_output(['git', 'rev-parse', 'HEAD:data/collection.json.gz'], cwd=ROOT, text=True).strip()
    if oid != current:
        raise RuntimeError('Use the current tracked compressed snapshot; an uncompressed cache may be stale.')
    return {'count': len(songs), 'songs': songs, 'git_blob': oid,
            'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'synced_at': document['synced_at'], 'membership_sha256': document['membership_sha256']}


def track_artists(track):
    return {normalize(a['name']) for a in track.get('artists', []) if a.get('name')}


def duration_close(song, track, loose=False):
    a, b = song.get('length_ms'), track.get('duration_ms')
    if not a or not b:
        return True
    return abs(a - b) <= max(15000 if loose else 8000, a * (0.08 if loose else 0.05))


def shared_isrc(song, track):
    value = track.get('external_ids', {}).get('isrc')
    return bool(value and value in song['isrcs'])


def same_song(song, track):
    artists = track_artists(track)
    if not artists.intersection(song['lead_keys'] or song['artist_keys']):
        return False
    full, base = normalize(track.get('name', '')), normalize(base_title(track.get('name', '')))
    if full not in song['title_keys'] and base not in song['base_keys'] and not supported_title_match(song, track):
        return False
    if base in {'intro', 'interlude', 'outro', 'untitled'} and not shared_isrc(song, track):
        return duration_close(song, track)
    return True


def rank_track(song, track):
    duration = abs((song.get('length_ms') or track.get('duration_ms', 0)) - track.get('duration_ms', 0))
    return (int(shared_isrc(song, track)), int(normalize(track.get('name', '')) in song['title_keys']),
            int(version_kind(track.get('name', '')) == song['version']), -duration)


def choice(song, identifier, method, track=None):
    result = {'mbid': song['mbid'], 'title': song['title'], 'artist': song['artist'],
              'spotify_id': identifier, 'method': method, 'status': 'matched' if identifier else 'unmatched'}
    if track:
        result.update({'spotify_title': track.get('name'),
                       'spotify_artists': [a['name'] for a in track.get('artists', [])],
                       'spotify_isrc': track.get('external_ids', {}).get('isrc')})
    return result


class LibraryIndex:
    def __init__(self, snapshot):
        self.items = snapshot['items']
        self.by_id, self.by_title, self.by_isrc = defaultdict(list), defaultdict(list), defaultdict(list)
        for item in self.items:
            track = item['track']
            for identifier in aliases(item):
                self.by_id[identifier].append(item)
            for key in {normalize(track['name']), normalize(base_title(track['name']))}:
                self.by_title[key].append(item)
            isrc = track.get('external_ids', {}).get('isrc')
            if isrc:
                self.by_isrc[isrc].append(item)

    def existing(self, song, ignore_links=False):
        if song['spotify_ids'] and not ignore_links:
            candidates = {saved_id(item): item for identifier in song['spotify_ids']
                          for item in self.by_id.get(identifier, [])}
            method = 'metadata_link_already_liked'
        else:
            pool = [item for key in song['base_keys'] + song['title_keys'] for item in self.by_title.get(key, [])]
            pool.extend(item for isrc in song['isrcs'] for item in self.by_isrc.get(isrc, []))
            candidates = {saved_id(item): item for item in pool if same_song(song, item['track'])}
            method = 'existing_song_version'
        if not candidates:
            return None
        # Highest metadata agreement, then preserve the oldest saved version.
        best = sorted(candidates.values(), key=lambda item: (item.get('added_at', ''), saved_id(item)))
        best = max(best, key=lambda item: rank_track(song, item['track']))
        return choice(song, saved_id(best), method, best['track'])


class CacheMiss(Exception):
    pass


class Catalog:
    def __init__(self, api, run_dir, index, cache_only=False):
        self.api, self.run_dir, self.index = api, run_dir, index
        self.cache_only = cache_only

    def track(self, identifier):
        path = self.run_dir / 'catalog/tracks' / (identifier + '.json')
        if path.exists():
            return read_json(path)
        if self.cache_only:
            raise CacheMiss()
        value = self.api.request('tracks/' + identifier, missing_ok=True)
        save_json(path, value)
        return value

    def search(self, query):
        key = hashlib.sha256(query.encode()).hexdigest()
        path = self.run_dir / 'catalog/search' / (key + '.json')
        if path.exists():
            return read_json(path)
        if self.cache_only:
            raise CacheMiss()
        response = self.api.request('search', {'q': query, 'type': 'track', 'limit': 10})
        rows = response.get('tracks', {}).get('items', [])
        save_json(path, rows)
        return rows

    @staticmethod
    def playable(track):
        return bool(track and track.get('id') and track.get('is_playable') is not False
                    and not track.get('restrictions'))

    def resolve(self, song):
        if song['spotify_ids']:
            tracks = [track for identifier in song['spotify_ids']
                      if self.playable(track := self.track(identifier))]
            if tracks:
                track = max(tracks, key=lambda row: rank_track(song, row))
                if track['id'] in self.index.by_id:
                    existing = self.index.by_id[track['id']][0]
                    return choice(song, saved_id(existing), 'metadata_link_relinked_already_liked', track)
                # Record both the linked ID and Spotify's playable ID for this account.
                identifier = next(i for i in song['spotify_ids']
                                  if (row := self.track(i)) and row['id'] == track['id'])
                result = choice(song, track['id'], 'metadata_link', track)
                result['metadata_spotify_id'] = identifier
                return result
            existing = self.index.existing(song, ignore_links=True)
            if existing:
                existing['method'] = 'existing_version_after_unavailable_metadata_link'
                return existing
        candidates = []
        queries = ['isrc:' + code for code in song['isrcs'][:4]]
        artist = (song['artist_names'] or [''])[0].replace('"', '')
        title = song['query_title'].replace('"', '')
        queries.append(f'track:"{title}" artist:"{artist}"')
        queries.append(f'{title} {artist}')
        for query in dict.fromkeys(queries):
            for track in self.search(query):
                if not self.playable(track) or not same_song(song, track):
                    continue
                if shared_isrc(song, track) and duration_close(song, track, loose=True):
                    candidates.append(track)
                elif version_kind(track['name']) == song['version'] and duration_close(song, track):
                    candidates.append(track)
            if candidates:
                track = max(candidates, key=lambda row: rank_track(song, row))
                return choice(song, track['id'], 'catalog_isrc' if shared_isrc(song, track) else 'catalog_artist_title_duration', track)
        result = choice(song, None, 'not_found_or_unavailable')
        return result


def make_plan(api, run_dir, source_path, resolve=False, cache_only=False):
    if (run_dir / 'changes.json').exists():
        raise RuntimeError('This run has a mutation journal; resume apply instead of replacing its plan.')
    snapshot = read_json(run_dir / 'spotify-before.json.gz')
    if snapshot['account']['id'] != api.auth['user_id']:
        raise RuntimeError('Backup belongs to a different Spotify account.')
    source = load_source(source_path)
    save_json(run_dir / 'source.json.gz', source)
    index = LibraryIndex(snapshot)
    catalog = Catalog(api, run_dir, index, cache_only=cache_only)
    decisions = []
    overrides_path = run_dir / 'overrides.json'
    overrides = read_json(overrides_path) if overrides_path.exists() else {}
    for number, song in enumerate(source['songs'], 1):
        if song['mbid'] in overrides:
            override = overrides[song['mbid']]
            decision = choice(song, override['spotify_id'], 'reviewed_override')
        else:
            decision = index.existing(song)
            if decision is None:
                try:
                    decision = catalog.resolve(song) if resolve else choice(song, None, 'needs_catalog_lookup')
                except CacheMiss:
                    decision = choice(song, None, 'needs_catalog_lookup')
        decisions.append(decision)
        if number % 100 == 0:
            matched = sum(d['status'] == 'matched' for d in decisions)
            print(f'Matched {matched:,}/{number:,} source entries; API requests: {api.calls}', flush=True)
    wanted = sorted({d['spotify_id'] for d in decisions if d['spotify_id']})
    current = {saved_id(item) for item in snapshot['items']}
    plan = {'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'source': {k: v for k, v in source.items() if k != 'songs'}, 'account': snapshot['account'],
            'source_count': source['count'], 'current_count': len(current),
            'desired_ids': wanted, 'add_ids': sorted(set(wanted) - current),
            'remove_ids': sorted(current - set(wanted)), 'decisions': decisions,
            'catalog_resolved': resolve and not any(d['method'] == 'needs_catalog_lookup' for d in decisions)}
    if len(wanted) > source['count']:
        raise RuntimeError('Spotify target exceeds the MusicBrainz source count.')
    save_json(run_dir / 'plan.json.gz', plan)
    summary = {'source_count': source['count'], 'current_count': len(current),
               'target_count': len(wanted), 'to_add': len(plan['add_ids']), 'to_remove': len(plan['remove_ids']),
               'unmatched': sum(d['status'] != 'matched' for d in decisions),
               'pending_lookups': sum(d['method'] == 'needs_catalog_lookup' for d in decisions),
               'methods': dict(Counter(d['method'] for d in decisions)), 'api_requests': api.calls}
    save_json(run_dir / 'plan-summary.json', summary)
    write_reports(run_dir, plan, snapshot)
    print(json.dumps(summary, indent=2), flush=True)
    return plan


def write_csv(path, rows, fields):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')) else value
                             for key, value in row.items()})


def write_reports(run_dir, plan, snapshot):
    fields = ['mbid', 'artist', 'title', 'status', 'method', 'spotify_id', 'spotify_title', 'spotify_artists', 'spotify_isrc']
    write_csv(run_dir / 'matches.csv', plan['decisions'], fields)
    write_csv(run_dir / 'unmatched.csv', [d for d in plan['decisions'] if not d['spotify_id']], fields)
    remove = set(plan['remove_ids'])
    rows = [{'spotify_id': saved_id(i), 'title': i['track']['name'],
             'artist': ' / '.join(a['name'] for a in i['track']['artists']), 'added_at': i.get('added_at')}
            for i in snapshot['items'] if saved_id(i) in remove]
    write_csv(run_dir / 'to-remove.csv', rows, ['spotify_id', 'artist', 'title', 'added_at'])


def validate_plan(plan, source, backup, user_id):
    if not plan.get('catalog_resolved'):
        raise RuntimeError('Resolve missing catalog matches before applying any changes.')
    if not source['count'] or plan['source_count'] != source['count']:
        raise RuntimeError('Source count changed or is empty.')
    if plan['account']['id'] != user_id or backup['account']['id'] != user_id:
        raise RuntimeError('Spotify plan, backup, and authorization must use the same account.')
    if {d['mbid'] for d in plan['decisions']} != {s['mbid'] for s in source['songs']} \
            or len(plan['decisions']) != source['count']:
        raise RuntimeError('The plan does not cover every MusicBrainz entry exactly once.')
    desired = set(plan['desired_ids'])
    if len(desired) != len(plan['desired_ids']) or len(desired) > source['count'] \
            or any(not TRACK_ID.fullmatch(value) for value in desired):
        raise RuntimeError('Invalid Spotify target identifiers or target count.')
    if desired != {d['spotify_id'] for d in plan['decisions'] if d.get('spotify_id')}:
        raise RuntimeError('Planned tracks differ from the matched source entries.')
    before = {saved_id(item) for item in backup['items']}
    if len(before) != backup['total']:
        raise RuntimeError('Spotify backup is incomplete.')
    if set(plan['add_ids']) != desired - before or set(plan['remove_ids']) != before - desired:
        raise RuntimeError('Add/remove plan differs from the complete backup.')
    return before, desired


def assert_source_current(source):
    current = subprocess.check_output(['git', 'rev-parse', 'HEAD:data/collection.json.gz'], cwd=ROOT, text=True).strip()
    if current != source['git_blob']:
        raise RuntimeError('MusicBrainz source changed after planning; rebuild the plan first.')


def journal_expected(before, journal):
    return (before | set(journal['added'])) - set(journal['removed'])


def recover_inflight(before, actual, journal):
    expected = journal_expected(before, journal)
    pending = journal.get('inflight')
    if pending:
        batch = set(pending['ids'])
        if (actual ^ expected) - batch:
            raise RuntimeError('Spotify changed outside the interrupted batch; replan before continuing.')
        key = 'added' if pending['method'] == 'PUT' else 'removed'
        done = batch & actual if key == 'added' else batch - actual
        journal[key] = sorted(set(journal[key]) | done)
        journal['inflight'] = None
    if actual != journal_expected(before, journal):
        raise RuntimeError('Spotify changed outside this run; preserve the library and rebuild the plan.')


def change_batch(api, method, batch, journal, journal_path):
    if not 0 < len(batch) <= 40 or any(not TRACK_ID.fullmatch(value) for value in batch):
        raise RuntimeError('Only batches of up to 40 Spotify track identifiers are allowed.')
    journal['inflight'] = {'method': method, 'ids': batch}
    save_json(journal_path, journal)
    api.request('me/library', {'uris': ','.join('spotify:track:' + value for value in batch)}, method=method)
    key = 'added' if method == 'PUT' else 'removed'
    journal[key] = sorted(set(journal[key]) | set(batch))
    journal['inflight'] = None
    save_json(journal_path, journal)


def verify_snapshot(plan, backup, snapshot):
    desired = set(plan['desired_ids'])
    actual = {saved_id(item) for item in snapshot['items']}
    if actual != desired or snapshot['total'] > plan['source_count']:
        raise RuntimeError(f'Spotify verification failed: {len(desired - actual)} missing and {len(actual - desired)} extra tracks.')
    original_dates = {saved_id(item): item.get('added_at') for item in backup['items']}
    for item in snapshot['items']:
        identifier = saved_id(item)
        if identifier in original_dates and item.get('added_at') != original_dates[identifier]:
            raise RuntimeError('An existing retained track has a changed saved date.')


def finish_apply(run_dir, plan, backup, journal, final):
    save_json(run_dir / 'spotify-after.json.gz', final)
    verify_snapshot(plan, backup, final)
    journal['phase'] = 'complete'
    save_json(run_dir / 'changes.json', journal)
    summary = {'verified_at': dt.datetime.now(dt.timezone.utc).isoformat(),
               'source_count': plan['source_count'], 'spotify_count': final['total'],
               'added': len(journal['added']), 'removed': len(journal['removed']),
               'unmatched_source_entries': sum(not d['spotify_id'] for d in plan['decisions']),
               'missing_desired_tracks': 0, 'extra_tracks': 0, 'retained_dates_preserved': True}
    save_json(run_dir / 'result.json', summary)
    print(json.dumps(summary, indent=2), flush=True)
    return summary


def apply_plan(api, run_dir):
    plan = read_json(run_dir / 'plan.json.gz')
    source = read_json(run_dir / 'source.json.gz')
    backup = read_json(run_dir / 'spotify-before.json.gz')
    user_id = api.profile()['id']
    before, desired = validate_plan(plan, source, backup, user_id)
    assert_source_current(source)
    if 'user-library-modify' not in api.auth.get('scope', '').split():
        raise RuntimeError('Spotify authorization does not include library modification.')
    digest = hashlib.sha256(json.dumps({'source': source['git_blob'], 'user': user_id,
                                       'desired': sorted(desired)}, sort_keys=True).encode()).hexdigest()
    journal_path = run_dir / 'changes.json'
    journal = read_json(journal_path) if journal_path.exists() else {
        'plan_sha256': digest, 'added': [], 'removed': [], 'inflight': None, 'phase': 'adding'}
    if journal['plan_sha256'] != digest:
        raise RuntimeError('The plan changed after mutations began; review the existing journal.')
    current = library(api)
    save_json(run_dir / 'spotify-pre-apply.json.gz', current)
    recover_inflight(before, {saved_id(i) for i in current['items']}, journal)
    save_json(journal_path, journal)
    if journal['phase'] in ('verifying', 'complete'):
        return finish_apply(run_dir, plan, backup, journal, current)
    pending = sorted(desired - journal_expected(before, journal))
    for offset in range(0, len(pending), 40):
        change_batch(api, 'PUT', pending[offset:offset + 40], journal, journal_path)
        print(f'Added missing Spotify tracks: {len(journal["added"]):,}/{len(plan["add_ids"]):,}', flush=True)
    # Read back the entire library before removing anything. All desired tracks
    # must be present, and changes outside this run must be accounted for.
    after_add = library(api) if pending else current
    save_json(run_dir / 'spotify-after-additions.json.gz', after_add)
    actual = {saved_id(item) for item in after_add['items']}
    if not desired <= actual or actual != journal_expected(before, journal):
        raise RuntimeError('Added tracks are missing or Spotify changed externally; no further removals will run.')
    assert_source_current(source)
    journal['phase'] = 'removing'
    save_json(journal_path, journal)
    pending = sorted(actual - desired)
    for offset in range(0, len(pending), 40):
        change_batch(api, 'DELETE', pending[offset:offset + 40], journal, journal_path)
        print(f'Removed extra Spotify tracks: {len(journal["removed"]):,}/{len(plan["remove_ids"]):,}', flush=True)
    journal['phase'] = 'verifying'
    save_json(journal_path, journal)
    final = library(api)
    return finish_apply(run_dir, plan, backup, journal, final)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('backup', 'plan', 'apply', 'verify'))
    parser.add_argument('--auth', type=Path, default=AUTH)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=ROOT / 'data/collection.json.gz')
    parser.add_argument('--resolve', action='store_true', help='Search Spotify for entries absent from the backup.')
    parser.add_argument('--cached-only', action='store_true', help='Use saved catalog responses without any API requests.')
    parser.add_argument('--interval', type=float, default=2.1, help='Minimum seconds between API requests.')
    args = parser.parse_args()
    if args.cached_only and args.command != 'plan':
        parser.error('--cached-only is only valid for plan')
    if args.interval < 1:
        parser.error('--interval must be at least one second')
    api = Spotify(args.auth, interval=args.interval)
    args.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == 'backup':
        path = args.run_dir / 'spotify-before.json.gz'
        if path.exists():
            raise RuntimeError('A backup already exists; use a new run directory.')
        snapshot = library(api)
        save_json(path, snapshot)
        print(json.dumps({'backed_up': snapshot['total'], 'api_requests': api.calls,
                          'backup': str(path)}), flush=True)
    elif args.command == 'plan':
        make_plan(api, args.run_dir, args.source, resolve=args.resolve or args.cached_only, cache_only=args.cached_only)
    elif args.command == 'apply':
        apply_plan(api, args.run_dir)
    elif args.command == 'verify':
        plan = read_json(args.run_dir / 'plan.json.gz')
        backup = read_json(args.run_dir / 'spotify-before.json.gz')
        snapshot = library(api)
        verify_snapshot(plan, backup, snapshot)
        save_json(args.run_dir / 'spotify-verified.json.gz', snapshot)
        print('Verified exact Spotify membership and preserved saved dates.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except SpotifyCooldown as error:
        print(str(error), file=sys.stderr)
        sys.exit(75)
