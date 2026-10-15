#!/usr/bin/env python3
"""Read a MusicBrainz recording collection and publish a complete local snapshot."""
import argparse
import calendar
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import random
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
AUTH_PATH = Path.home() / '.config/musicbrainz-liked-songs/auth.json'
BASE = 'https://musicbrainz.org'
CSV_BASE_FIELDS = ['title', 'artist', 'recording_mbid', 'artist_mbids', 'length_ms', 'isrcs', 'work_mbids', 'works', 'disambiguation', 'video', 'musicbrainz_url']
METADATA_INCLUDES = '+'.join([
    'artist-credits', 'isrcs', 'aliases', 'annotation', 'tags', 'genres', 'ratings',
    'area-rels', 'artist-rels', 'event-rels', 'genre-rels', 'instrument-rels',
    'label-rels', 'place-rels', 'recording-rels', 'release-rels',
    'release-group-rels', 'series-rels', 'url-rels', 'work-rels', 'work-level-rels',
])


def atomic_write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def atomic_gzip(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'wb') as output:
            with gzip.GzipFile(filename='', mode='wb', compresslevel=6, fileobj=output, mtime=0) as compressed:
                for start in range(0, len(text), 1024 * 1024):
                    compressed.write(text[start:start + 1024 * 1024].encode('utf-8'))
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def load_snapshot():
    compressed = ROOT / 'data/collection.json.gz'
    if compressed.exists():
        with gzip.open(compressed, 'rt', encoding='utf-8') as source: return json.load(source)
    return json.loads((ROOT / 'data/collection.json').read_text())


def materialize_compressed_exports():
    for filename in ('collection.json', 'liked-songs.csv'):
        path = ROOT / 'data' / filename
        compressed = path.with_suffix(path.suffix + '.gz')
        if not compressed.exists(): continue
        fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, 'wb') as output, gzip.open(compressed, 'rb') as source:
                shutil.copyfileobj(source, output, 1024 * 1024)
            os.replace(name, path)
        finally:
            if os.path.exists(name): os.unlink(name)


class MusicBrainz:
    def __init__(self, config):
        self.config = config
        self.last_request = 0
        self.request_count = 0
        env = {k: os.environ.get('MUSICBRAINZ_' + k.upper()) for k in ('client_id', 'client_secret', 'refresh_token')}
        self.from_environment = all(env.values())
        if any(env.values()) and not self.from_environment:
            raise RuntimeError('Provide all three MUSICBRAINZ OAuth environment variables.')
        if self.from_environment:
            self.auth = env
        elif AUTH_PATH.exists():
            self.auth = json.loads(AUTH_PATH.read_text())
        else:
            raise RuntimeError('MusicBrainz credentials are missing. Run python3 scripts/authorize.py.')

    def refresh(self):
        body = {k: self.auth[k] for k in ('client_id', 'client_secret', 'refresh_token')}
        body['grant_type'] = 'refresh_token'
        token = self.request('/oauth2/token', body=body, authenticated=False)
        self.auth.update(token)
        self.auth['expires_at'] = time.time() + token.get('expires_in', 3600) - 60
        if not self.from_environment:
            atomic_write(AUTH_PATH, json.dumps(self.auth), 0o600)
        elif token.get('refresh_token') != body['refresh_token']:
            raise RuntimeError('OAuth refresh token changed. Reauthorize and update the GitHub secret before syncing again.')

    def request(self, path, params=None, body=None, authenticated=True):
        if authenticated and (not self.auth.get('access_token') or self.auth.get('expires_at', 0) <= time.time()):
            self.refresh()
        url = BASE + path
        if params: url += '?' + urllib.parse.urlencode(params)
        retried_auth = False
        for attempt in range(6):
            time.sleep(max(0, random.uniform(1.1, 3.0) - (time.monotonic() - self.last_request)))
            headers = {'User-Agent': self.config['user_agent'], 'Accept': 'application/json', 'Cache-Control': 'no-cache'}
            if authenticated: headers['Authorization'] = 'Bearer ' + self.auth['access_token']
            payload = None
            if body is not None:
                payload = urllib.parse.urlencode(body).encode()
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
            self.last_request = time.monotonic()
            self.request_count += 1
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=payload, headers=headers), timeout=45) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 401 and authenticated and not retried_auth:
                    retried_auth = True; self.refresh(); continue
                if e.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f'MusicBrainz HTTP {e.code}; snapshot left unchanged.') from None
                delay = e.headers.get('Retry-After', '')
                time.sleep(min(60, int(delay) if delay.isdigit() else 2 ** attempt))
            except (urllib.error.URLError, TimeoutError):
                time.sleep(min(30, 2 ** attempt))
        raise RuntimeError('MusicBrainz request failed after retries; snapshot left unchanged.')


def read_collection(api, config, full_metadata=True):
    cid = config['collection_id']
    metadata = api.request('/ws/2/collection/' + cid, {'fmt': 'json'})
    if metadata.get('id') != cid or metadata.get('editor') != config['owner'] or metadata.get('entity-type') != 'recording':
        raise RuntimeError('Collection ID, owner, or entity type does not match config.json.')
    count = metadata['recording-count']
    recordings = []
    offset = 0
    while offset < count:
        params = {'collection': cid, 'limit': 100, 'offset': offset, 'fmt': 'json'}
        if full_metadata: params['inc'] = METADATA_INCLUDES
        page = api.request('/ws/2/recording', params)
        rows = page.get('recordings', [])
        if page.get('recording-count') != count or not rows or page.get('recording-offset') != offset:
            raise RuntimeError(f'Collection changed or pagination was incomplete (expected count {count}, received {page.get("recording-count")}; expected offset {offset}, received {page.get("recording-offset")}; page size {len(rows)}). Retry sync.')
        recordings.extend(rows); offset += len(rows)
        print(f'Downloaded {offset:,} / {count:,} recordings', flush=True)
    end = api.request('/ws/2/collection/' + cid, {'fmt': 'json'})
    ids = [r['id'] for r in recordings]
    if end.get('recording-count') != count or len(ids) != count or len(set(ids)) != count:
        raise RuntimeError('Collection changed or contains overlapping pages. Retry sync.')
    return metadata, sorted(recordings, key=lambda r: r['id'])


def flatten_recording(r):
    credits = r.get('artist-credit', [])
    works = {rel['work']['id']: rel['work']['title'] for rel in r.get('relations', []) if rel.get('target-type') == 'work' and 'work' in rel}
    row = {
        'recording_mbid': r['id'], 'title': r['title'],
        'artist': ''.join(c.get('name', c.get('artist', {}).get('name', '')) + c.get('joinphrase', '') for c in credits),
        'artist_mbids': '; '.join(c['artist']['id'] for c in credits if 'artist' in c),
        'length_ms': r.get('length'), 'isrcs': '; '.join(sorted(set(r.get('isrcs', [])))),
        'work_mbids': '; '.join(sorted(works)), 'works': '; '.join(works[k] for k in sorted(works)),
        'disambiguation': r.get('disambiguation', ''), 'video': bool(r.get('video', False)),
        'musicbrainz_url': 'https://musicbrainz.org/recording/' + r['id'],
    }
    represented = {'id', 'title', 'artist-credit', 'length', 'isrcs', 'relations', 'disambiguation', 'video'}
    for key in sorted(r.keys() - represented):
        value = r[key]
        row[key.replace('-', '_')] = json_cell(value) if isinstance(value, (dict, list)) else value
    add_metadata_columns(row, 'artist_credit', credits, {'name', 'joinphrase', 'artist'})
    add_metadata_columns(row, 'artist', [c.get('artist', {}) for c in credits], {'id'})
    relations = r.get('relations', [])
    add_metadata_columns(row, 'relationship', relations, {'work'})
    add_metadata_columns(row, 'work', [rel.get('work', {}) for rel in relations], {'id', 'title'})
    if any('work' in rel for rel in relations):
        # Unlike work_mbids, this retains repeated targets in relationship order.
        row['relationship_work_mbids'] = json_cell([rel.get('work', {}).get('id') for rel in relations])
    return row


def json_cell(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def add_metadata_columns(row, prefix, objects, represented):
    keys = set().union(*(obj.keys() for obj in objects)) - represented
    for key in sorted(keys):
        # Parallel JSON arrays preserve missing values and nested structures.
        row[prefix + '_' + key.replace('-', '_')] = json_cell([obj.get(key) for obj in objects])


def csv_fields(flat):
    extra = set().union(*(row.keys() for row in flat)) - set(CSV_BASE_FIELDS)
    return CSV_BASE_FIELDS + sorted(extra)


def flatten_snapshot(data):
    flat = [flatten_recording(r) for r in data['recordings']]
    collection = data.get('collection_metadata', {
        'id': data['collection_id'], 'name': data['collection_name'],
        'editor': data['collection_owner'], 'recording-count': data['count'],
    })
    for row in flat:
        for key, value in collection.items():
            row['collection_' + key.replace('-', '_')] = json_cell(value) if isinstance(value, (dict, list)) else value
    flat.sort(key=lambda r: (r['title'].casefold(), r['artist'].casefold(), r['recording_mbid']))
    return flat


def build_exports(data=None):
    if data is None: data = load_snapshot()
    flat = flatten_snapshot(data)
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=csv_fields(flat)); writer.writeheader()
    writer.writerows({k: csv_cell(v) for k, v in row.items()} for row in flat)
    csv_text = '\ufeff' + out.getvalue()
    atomic_write(ROOT / 'data/liked-songs.csv', csv_text)
    atomic_gzip(ROOT / 'data/liked-songs.csv.gz', csv_text)
    # Keep download metadata separate so the searchable page stays small.
    for prefix in '0123456789abcdef':
        rows = [row for row in flat if row['recording_mbid'].startswith(prefix)]
        atomic_write(ROOT / 'site/csv' / (prefix + '.json'), json_cell(rows) + '\n')
    build_page(data, flat)


def csv_cell(value):
    if isinstance(value, bool): return 'true' if value else 'false'
    if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
        return "'" + value
    return value


def write_snapshot(metadata, recordings, config, rebuild_exports=True):
    # No output changes until the entire paginated download has been verified.
    ids = sorted(r['id'] for r in recordings)
    data = {
        'collection_id': config['collection_id'], 'collection_name': metadata.get('name', config['name']),
        'collection_owner': config['owner'], 'synced_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'count': len(recordings), 'membership_sha256': hashlib.sha256('\n'.join(ids).encode()).hexdigest(),
        'metadata_refreshed_at': config.get('metadata_refreshed_at', dt.datetime.now(dt.timezone.utc).isoformat()),
        'collection_metadata': metadata,
        'metadata_includes': METADATA_INCLUDES,
        'recordings': recordings,
    }
    snapshot = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    atomic_write(ROOT / 'data/collection.json', snapshot)
    atomic_gzip(ROOT / 'data/collection.json.gz', snapshot)
    if rebuild_exports: build_exports(data)
    print(f'Verified and saved {len(recordings):,} recordings. Membership SHA-256: {data["membership_sha256"]}')


def metadata_refresh_due(refreshed_at, now):
    """Refresh after two calendar months, clamping dates at month end."""
    month = refreshed_at.month - 1 + 2
    year, month = refreshed_at.year + month // 12, month % 12 + 1
    day = min(refreshed_at.day, calendar.monthrange(year, month)[1])
    return now >= refreshed_at.replace(year=year, month=month, day=day)


def synchronize(api, config, refresh_metadata=False):
    cached_path = ROOT / 'data/collection.json'
    cached = load_snapshot() if cached_path.exists() or (ROOT / 'data/collection.json.gz').exists() else None
    if cached and (cached['collection_id'] != config['collection_id'] or cached['collection_owner'] != config['owner']):
        raise RuntimeError('Saved snapshot belongs to a different collection.')
    now = dt.datetime.now(dt.timezone.utc)
    refreshed_at = cached.get('metadata_refreshed_at', cached['synced_at']) if cached else None
    full_metadata = refresh_metadata or not cached or metadata_refresh_due(dt.datetime.fromisoformat(refreshed_at), now) or cached.get('metadata_includes') != METADATA_INCLUDES
    metadata, current = read_collection(api, config, full_metadata=full_metadata)
    old = {r['id']: r for r in cached['recordings']} if cached else {}
    current_ids = {r['id'] for r in current}
    added = current_ids - old.keys()
    removed = old.keys() - current_ids
    if full_metadata:
        recordings = current
    else:
        records = {rid: old[rid] for rid in current_ids if rid in old}
        for rid in sorted(added):
            r = api.request('/ws/2/recording/' + rid, {'inc': METADATA_INCLUDES, 'fmt': 'json'})
            if r.get('id') != rid: raise RuntimeError('Recording merged during sync. Retry sync.')
            records[rid] = r
        recordings = sorted(records.values(), key=lambda r: r['id'])
    changed = not cached or recordings != cached['recordings'] or metadata != cached.get('collection_metadata') or cached.get('metadata_includes') != METADATA_INCLUDES
    if changed or full_metadata:
        next_config = dict(config, metadata_refreshed_at=now.isoformat() if full_metadata else refreshed_at)
        # An unchanged metadata refresh still advances the freshness timestamp,
        # but need not rebuild the large CSV, download shards, or search page.
        write_snapshot(metadata, recordings, next_config, rebuild_exports=changed)
    else:
        print('No collection changes; reused all metadata and left JSON, CSV, and search page unchanged.')
    fetched = len(recordings) if full_metadata else len(added)
    status = {'checked_at': now.isoformat(), 'count': len(recordings), 'added': len(added), 'removed': len(removed), 'metadata_refreshed': full_metadata, 'metadata_fetched': fetched, 'metadata_reused': len(recordings) - fetched, 'content_changed': changed}
    if hasattr(api, 'request_count'): status['api_requests'] = api.request_count
    for path in (ROOT / 'data/sync-status.json', ROOT / 'site/sync-status.json'):
        atomic_write(path, json.dumps(status, indent=2) + '\n')
    print(f'Changes: +{len(added)} / -{len(removed)}. Full metadata refresh: {full_metadata}')
    print(f'Expanded metadata fetched: {fetched:,}; reused: {len(recordings) - fetched:,}.')
    if 'api_requests' in status: print(f'API requests (including authentication and retries): {status["api_requests"]}')
    return status


def build_page(data=None, flat=None):
    if data is None: data = load_snapshot()
    if flat is None: flat = flatten_snapshot(data)
    payload = {k: data[k] for k in ('collection_id', 'collection_name', 'collection_owner', 'synced_at', 'count')}
    payload['songs'] = []
    for row in flat:
        song = {key: row[key] for key in CSV_BASE_FIELDS}
        song['first_release_date'] = row.get('first_release_date') or ''
        languages = {code for codes in json.loads(row.get('work_languages', '[]')) if isinstance(codes, list) for code in codes if code}
        languages.update(code for code in json.loads(row.get('work_language', '[]')) if code)
        song['languages'] = sorted(languages)
        payload['songs'].append(song)
    payload['csv_fields'] = csv_fields(flat)
    # Untrusted names must not terminate the embedded JSON script element.
    encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    template = (ROOT / 'scripts/page.html').read_text()
    atomic_write(ROOT / 'site/index.html', template.replace('<!-- COLLECTION_DATA -->', encoded))
    atomic_write(ROOT / 'site/.nojekyll', '')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-only', action='store_true', help='Rebuild the page and CSV exports from the existing snapshot, without network access.')
    parser.add_argument('--refresh-metadata', action='store_true', help='Refresh every recording, rather than reusing cached metadata.')
    args = parser.parse_args()
    if args.build_only: return build_exports()
    config = json.loads((ROOT / 'config.json').read_text())
    api = MusicBrainz(config)
    synchronize(api, config, args.refresh_metadata)


if __name__ == '__main__':
    try: main()
    except Exception as e:
        # Do not include HTTP request/response bodies or credentials in error logs.
        print(f'Sync failed: {e}', file=sys.stderr); sys.exit(1)
