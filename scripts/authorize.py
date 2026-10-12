#!/usr/bin/env python3
"""Authorize the local sync, optionally installing credentials in GitHub secrets."""
import argparse
import base64
import getpass
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import secrets
import subprocess
import time
import urllib.parse
import urllib.request
import webbrowser
import sync


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--github', action='store_true', help='Set OAuth secrets for the repository using gh.')
    args = parser.parse_args()
    config = json.loads((sync.ROOT / 'config.json').read_text())
    auth = json.loads(sync.AUTH_PATH.read_text()) if sync.AUTH_PATH.exists() else {}
    redirect = 'http://127.0.0.1:8767/callback'
    if not auth.get('client_id'):
        print('Register a Web application at https://musicbrainz.org/account/applications/register')
        print('Redirect URI:', redirect)
        auth['client_id'] = input('OAuth client ID: ').strip()
        auth['client_secret'] = getpass.getpass('OAuth client secret (hidden): ').strip()
    if not all(auth.get(k) for k in ('client_id', 'client_secret')):
        raise RuntimeError('Missing client credentials.')
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    outcome = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path != '/callback' or not secrets.compare_digest(q.get('state', [''])[0], state):
                self.send_response(403); self.end_headers(); return
            try:
                if 'error' in q: raise RuntimeError('MusicBrainz authorization was denied.')
                body = {'grant_type': 'authorization_code', 'code': q['code'][0], 'client_id': auth['client_id'], 'client_secret': auth['client_secret'], 'redirect_uri': redirect, 'code_verifier': verifier}
                req = urllib.request.Request(sync.BASE + '/oauth2/token', data=urllib.parse.urlencode(body).encode(), headers={'User-Agent': config['user_agent']})
                with urllib.request.urlopen(req, timeout=45) as r: token = json.load(r)
                if not token.get('refresh_token'): raise RuntimeError('MusicBrainz did not provide offline access.')
                auth.update(token); auth['expires_at'] = time.time() + token.get('expires_in', 3600) - 60
                sync.AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
                os.chmod(sync.AUTH_PATH.parent, 0o700)
                sync.atomic_write(sync.AUTH_PATH, json.dumps(auth), 0o600)
                if args.github:
                    for name, key in [('MUSICBRAINZ_CLIENT_ID', 'client_id'), ('MUSICBRAINZ_CLIENT_SECRET', 'client_secret'), ('MUSICBRAINZ_REFRESH_TOKEN', 'refresh_token')]:
                        p = subprocess.run(['gh', 'secret', 'set', name, '--repo', config['repository']], input=auth[key], text=True, capture_output=True)
                        if p.returncode: raise RuntimeError('Could not update GitHub secret ' + name)
                outcome['ok'] = True
                self.send_response(200); self.send_header('Content-Type', 'text/plain'); self.end_headers()
                self.wfile.write(b'Authorization complete. You may close this tab.')
            except Exception as e:
                outcome['error'] = type(e).__name__
                self.send_response(500); self.end_headers(); self.wfile.write(b'Authorization failed. Check the terminal.')

    query = {'response_type': 'code', 'client_id': auth['client_id'], 'redirect_uri': redirect, 'scope': 'collection', 'access_type': 'offline', 'approval_prompt': 'force', 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
    with HTTPServer(('127.0.0.1', 8767), Callback) as server:
        server.timeout = 1
        webbrowser.open(sync.BASE + '/oauth2/authorize?' + urllib.parse.urlencode(query))
        print('Approve collection access in your browser. Waiting up to five minutes…')
        deadline = time.monotonic() + 300
        while not outcome and time.monotonic() < deadline: server.handle_request()
    if not outcome.get('ok'): raise RuntimeError('Authorization failed or timed out. Credentials were not logged.')
    print('Credentials saved locally' + (' and in GitHub Actions secrets.' if args.github else '.'))


if __name__ == '__main__':
    try: main()
    except Exception as e:
        print('Authorization failed:', e); raise SystemExit(1)
