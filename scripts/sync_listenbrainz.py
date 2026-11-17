#!/usr/bin/env python3
"""Mirror a verified MusicBrainz collection to ListenBrainz loves (dry-run by default)."""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from sync import ROOT, MusicBrainz, atomic_write, read_collection

API = 'https://api.listenbrainz.org/1/'
STATE_VERSION = 1


class BudgetExpired(Exception):
    pass


def recording_ids(values):
    ids = set()
    for value in values:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError('Expected canonical recording MBIDs.')
        if value in ids:
            raise ValueError('Duplicate recording MBID; refusing an incomplete or ambiguous snapshot.')
        ids.add(value)
    return ids


def state_identity(config):
    return {
        'version': STATE_VERSION,
        'collection_id': config['collection_id'],
        'musicbrainz_owner': config['owner'],
        'listenbrainz_user': config.get('listenbrainz_user', config['owner']),
    }


def load_state(path, config):
    if not path.exists():
        return dict(state_identity(config), managed_recording_mbids=[])
    state = json.loads(path.read_text())
    if any(state.get(key) != value for key, value in state_identity(config).items()):
        raise ValueError('ListenBrainz state belongs to a different collection/account or schema.')
    recording_ids(state['managed_recording_mbids'])
    return state


def snapshot_ids(path, config):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as source:
        snapshot = json.load(source)
    if snapshot.get('collection_id') != config['collection_id'] or snapshot.get('collection_owner') != config['owner']:
        raise ValueError('Collection snapshot belongs to a different collection/account.')
    ids = recording_ids([row['id'] for row in snapshot['recordings']])
    digest = hashlib.sha256('\n'.join(sorted(ids)).encode()).hexdigest()
    if snapshot.get('count') != len(ids) or snapshot.get('membership_sha256') != digest:
        raise ValueError('Collection snapshot count/checksum mismatch; refusing feedback changes.')
    return sorted(ids)


class ListenBrainz:
    def __init__(self, config, token=None, budget_seconds=1800):
        self.user = config.get('listenbrainz_user', config['owner'])
        self.user_agent = config['user_agent'] + ' ListenBrainzCollectionSync/1.0'
        self.token = token
        self.deadline = time.monotonic() + budget_seconds
        self.next_request = 0

    def wait(self, seconds):
        if time.monotonic() + seconds >= self.deadline:
            raise BudgetExpired()
        time.sleep(seconds)

    def request(self, path, body=None):
        for attempt in range(7):
            self.wait(max(0, self.next_request - time.monotonic()))
            headers = {'User-Agent': self.user_agent, 'Accept': 'application/json'}
            if self.token:
                headers['Authorization'] = 'Token ' + self.token
            if body is not None:
                headers['Content-Type'] = 'application/json'
            payload = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(API + path, data=payload, headers=headers)
            self.next_request = time.monotonic() + 0.4
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    result = json.load(response)
                    remaining = response.headers.get('X-RateLimit-Remaining', '')
                    reset = response.headers.get('X-RateLimit-Reset-In', '')
                    if remaining.isdigit() and int(remaining) < 2 and reset.isdigit():
                        self.next_request = time.monotonic() + int(reset) + 1
                    return result
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504):
                    # Never include request headers, credentials, or arbitrary response bodies.
                    raise RuntimeError(f'ListenBrainz HTTP {error.code}.') from None
                delay = error.headers.get('Retry-After') or error.headers.get('X-RateLimit-Reset-In', '')
                self.wait(int(delay) + 1 if delay.isdigit() else min(60, 2 ** attempt))
            except (urllib.error.URLError, TimeoutError, socket.timeout):
                self.wait(min(30, 2 ** attempt))
        raise RuntimeError('ListenBrainz unavailable after retries; next run will reconcile again.')

    def validate_token(self):
        if not self.token:
            raise ValueError('--apply requires LISTENBRAINZ_TOKEN.')
        result = self.request('validate-token')
        if not result.get('valid') or result.get('user_name') != self.user:
            raise ValueError('ListenBrainz token does not belong to the configured user.')

    def feedback(self):
        scores, seen = {}, set()
        offset, expected_total = 0, None
        while True:
            query = urllib.parse.urlencode({'count': 1000, 'offset': offset, 'metadata': 'false'})
            page = self.request('feedback/user/' + urllib.parse.quote(self.user, safe='') + '/get-feedback?' + query)
            total, rows = page['total_count'], page['feedback']
            if type(total) is not int or total < 0 or page.get('offset') != offset:
                raise ValueError('Invalid ListenBrainz pagination.')
            if expected_total is not None and total != expected_total:
                raise ValueError('ListenBrainz feedback changed while reading; retry next run.')
            expected_total = total
            for row in rows:
                key = (row.get('recording_mbid'), row.get('recording_msid'))
                if key == (None, None) or key in seen or type(row.get('score')) is not int or row['score'] not in (-1, 1):
                    raise ValueError('Duplicate or invalid ListenBrainz feedback.')
                seen.add(key)
                mbid = row.get('recording_mbid')
                if mbid:
                    recording_ids([mbid])
                    if mbid in scores:
                        raise ValueError('Multiple feedback entries for one recording; manual review needed.')
                    scores[mbid] = row['score']
            offset += len(rows)
            if offset == total:
                return scores
            if not rows or offset > total:
                raise ValueError('Incomplete ListenBrainz feedback; refusing changes.')

    def set_score(self, mbid, score):
        result = self.request('feedback/recording-feedback', {'recording_mbid': mbid, 'score': score})
        if result.get('status') != 'ok':
            raise ValueError('ListenBrainz did not acknowledge feedback.')


def reconcile(api, config, desired, state, save, apply=False, max_writes=5000, publish=None):
    """Use actual target feedback for retries; state records which MBIDs we own."""
    if max_writes < 1:
        raise ValueError('max_writes must be positive.')
    desired = recording_ids(desired)
    managed = recording_ids(state['managed_recording_mbids'])
    if apply:
        api.validate_token()
    scores = api.feedback()
    additions = sorted(mbid for mbid in desired if scores.get(mbid) != 1)
    removals = sorted(mbid for mbid in managed - desired if scores.get(mbid) == 1)
    # Forget removed entries that are already neutral or explicitly hated.
    managed = desired | set(removals)
    result = {'additions': len(additions), 'removals': len(removals), 'applied': 0,
              'pending': len(additions) + len(removals), 'dry_run': not apply}
    print(f"ListenBrainz plan: +{len(additions):,} / -{len(removals):,}", flush=True)
    if not apply:
        return result

    state = dict(state, **state_identity(config))
    def checkpoint(durable=False):
        state['managed_recording_mbids'] = sorted(managed)
        state['last_run'] = result.copy()
        state['checked_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        save(state)
        if durable and publish:
            publish()

    # Claim intended additions BEFORE writing: a killed process cannot orphan a love
    # that must be removed if its source entry disappears before the next run.
    checkpoint(durable=True)
    try:
        for mbid, score in [(m, 0) for m in removals] + [(m, 1) for m in additions]:
            if result['applied'] >= max_writes:
                break
            api.set_score(mbid, score)
            if score == 0:
                managed.discard(mbid)
            result['applied'] += 1
            result['pending'] -= 1
            if result['applied'] % 100 == 0:
                checkpoint()
                print(f"Applied {result['applied']:,}; pending {result['pending']:,}", flush=True)
    except BudgetExpired:
        print('Run budget reached; remaining work resumes on the next run.', flush=True)
    finally:
        checkpoint(durable=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Write feedback; default only reports the plan.')
    parser.add_argument('--state', type=Path, default=ROOT / '.cache/listenbrainz-state.json')
    parser.add_argument('--snapshot', type=Path, help='Use the collection snapshot verified by the weekly workflow.')
    parser.add_argument('--max-writes', type=int, default=5000)
    parser.add_argument('--budget-seconds', type=int, default=1800)
    parser.add_argument('--publish-state', action='store_true', help='Checkpoint to the dedicated Git branch (GitHub Actions).')
    args = parser.parse_args()
    if args.max_writes < 1 or args.budget_seconds < 1:
        parser.error('Write limit and budget must be positive.')
    config = json.loads((ROOT / 'config.json').read_text())
    state = load_state(args.state, config)
    if args.snapshot:
        desired = snapshot_ids(args.snapshot, config)
    else:
        # Local previews can still read live membership without expanded metadata.
        _, rows = read_collection(MusicBrainz(config), config, full_metadata=False)
        desired = [row['id'] for row in rows]
    api = ListenBrainz(config, os.environ.get('LISTENBRAINZ_TOKEN'), args.budget_seconds)
    result = reconcile(api, config, desired, state,
                       lambda value: atomic_write(args.state, json.dumps(value, indent=2) + '\n'),
                       apply=args.apply, max_writes=args.max_writes,
                       publish=(lambda: subprocess.run(['bash', str(ROOT / 'scripts/publish_listenbrainz_state.sh'),
                                                        str(args.state)], check=True)) if args.publish_state else None)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
