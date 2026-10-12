#!/usr/bin/env python3
"""Read a MusicBrainz recording collection and publish a complete local snapshot."""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
AUTH_PATH = Path.home() / '.config/musicbrainz-liked-songs/auth.json'
BASE = 'https://musicbrainz.org'


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


class MusicBrainz:
    def __init__(self, config):
        self.config = config
        self.last_request = 0
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
            time.sleep(max(0, 1.1 - (time.monotonic() - self.last_request)))
            headers = {'User-Agent': self.config['user_agent'], 'Accept': 'application/json'}
            if authenticated: headers['Authorization'] = 'Bearer ' + self.auth['access_token']
            payload = None
            if body is not None:
                payload = urllib.parse.urlencode(body).encode()
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
            self.last_request = time.monotonic()
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
        if full_metadata: params['inc'] = 'artist-credits+isrcs+work-rels'
        page = api.request('/ws/2/recording', params)
        rows = page.get('recordings', [])
        if page.get('recording-count') != count or not rows or page.get('recording-offset') != offset:
            raise RuntimeError('Collection changed or pagination was incomplete. Retry sync.')
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
    return {
        'recording_mbid': r['id'], 'title': r['title'],
        'artist': ''.join(c.get('name', c.get('artist', {}).get('name', '')) + c.get('joinphrase', '') for c in credits),
        'artist_mbids': '; '.join(c['artist']['id'] for c in credits if 'artist' in c),
        'length_ms': r.get('length'), 'isrcs': '; '.join(sorted(set(r.get('isrcs', [])))),
        'work_mbids': '; '.join(sorted(works)), 'works': '; '.join(works[k] for k in sorted(works)),
        'disambiguation': r.get('disambiguation', ''), 'video': bool(r.get('video', False)),
        'musicbrainz_url': 'https://musicbrainz.org/recording/' + r['id'],
    }


def csv_cell(value):
    if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
        return "'" + value
    return value


def write_snapshot(metadata, recordings, config):
    # No output changes until the entire paginated download has been verified.
    ids = sorted(r['id'] for r in recordings)
    data = {
        'collection_id': config['collection_id'], 'collection_name': metadata.get('name', config['name']),
        'collection_owner': config['owner'], 'synced_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'count': len(recordings), 'membership_sha256': hashlib.sha256('\n'.join(ids).encode()).hexdigest(),
        'metadata_refreshed_at': config.get('metadata_refreshed_at', dt.datetime.now(dt.timezone.utc).isoformat()),
        'recordings': recordings,
    }
    flat = [flatten_recording(r) for r in recordings]
    flat.sort(key=lambda r: (r['title'].casefold(), r['artist'].casefold(), r['recording_mbid']))
    out = io.StringIO(newline='')
    fields = list(flatten_recording({'id': '', 'title': ''}))
    writer = csv.DictWriter(out, fieldnames=fields); writer.writeheader()
    writer.writerows({k: csv_cell(v) for k, v in row.items()} for row in flat)
    atomic_write(ROOT / 'data/collection.json', json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    atomic_write(ROOT / 'data/liked-songs.csv', '\ufeff' + out.getvalue())
    build_page(data, flat)
    print(f'Verified and saved {len(recordings):,} recordings. Membership SHA-256: {data["membership_sha256"]}')


def synchronize(api, config, refresh_metadata=False):
    cached_path = ROOT / 'data/collection.json'
    cached = json.loads(cached_path.read_text()) if cached_path.exists() else None
    if cached and (cached['collection_id'] != config['collection_id'] or cached['collection_owner'] != config['owner']):
        raise RuntimeError('Saved snapshot belongs to a different collection.')
    now = dt.datetime.now(dt.timezone.utc)
    age = (now - dt.datetime.fromisoformat(cached.get('metadata_refreshed_at', cached['synced_at']))).total_seconds() if cached else float('inf')
    full_metadata = refresh_metadata or age >= 7 * 86400
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
            r = api.request('/ws/2/recording/' + rid, {'inc': 'artist-credits+isrcs+work-rels', 'fmt': 'json'})
            if r.get('id') != rid: raise RuntimeError('Recording merged during sync. Retry sync.')
            records[rid] = r
        recordings = sorted(records.values(), key=lambda r: r['id'])
    changed = not cached or recordings != cached['recordings'] or metadata.get('name') != cached['collection_name']
    if changed or full_metadata:
        next_config = dict(config, metadata_refreshed_at=now.isoformat() if full_metadata else cached['metadata_refreshed_at'])
        write_snapshot(metadata, recordings, next_config)
    else:
        print('No collection changes; reused all metadata and left JSON, CSV, and search page unchanged.')
    status = {'checked_at': now.isoformat(), 'count': len(recordings), 'added': len(added), 'removed': len(removed), 'metadata_refreshed': full_metadata, 'content_changed': changed}
    for path in (ROOT / 'data/sync-status.json', ROOT / 'site/sync-status.json'):
        atomic_write(path, json.dumps(status, indent=2) + '\n')
    print(f'Changes: +{len(added)} / -{len(removed)}. Full metadata refresh: {full_metadata}')
    return status


def build_page(data=None, flat=None):
    if data is None: data = json.loads((ROOT / 'data/collection.json').read_text())
    if flat is None: flat = sorted((flatten_recording(r) for r in data['recordings']), key=lambda r: (r['title'].casefold(), r['artist'].casefold(), r['recording_mbid']))
    payload = {k: data[k] for k in ('collection_id', 'collection_name', 'collection_owner', 'synced_at', 'count')}
    payload['songs'] = flat
    # Untrusted names must not terminate the embedded JSON script element.
    encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    template = (ROOT / 'scripts/page.html').read_text()
    atomic_write(ROOT / 'site/index.html', template.replace('<!-- COLLECTION_DATA -->', encoded))
    atomic_write(ROOT / 'site/.nojekyll', '')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-only', action='store_true', help='Rebuild the page from the existing snapshot, without network access.')
    parser.add_argument('--refresh-metadata', action='store_true', help='Refresh every recording, rather than reusing cached metadata.')
    args = parser.parse_args()
    if args.build_only: return build_page()
    config = json.loads((ROOT / 'config.json').read_text())
    api = MusicBrainz(config)
    synchronize(api, config, args.refresh_metadata)


if __name__ == '__main__':
    try: main()
    except Exception as e:
        # Do not include HTTP request/response bodies or credentials in error logs.
        print(f'Sync failed: {e}', file=sys.stderr); sys.exit(1)
