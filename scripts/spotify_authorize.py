#!/usr/bin/env python3
"""Authorize read/write access to Spotify Liked Songs using OAuth PKCE.

Put the Spotify client_id in ~/.config/liked-songs-spotify/auth.json, register
http://127.0.0.1:8766/callback, then run this script with --github-secrets.
Credentials stay outside the repository or in encrypted GitHub Actions secrets.
"""
import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH = Path.home() / '.config/liked-songs-spotify/auth.json'
SCOPES = {'user-library-read', 'user-library-modify'}


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.auth-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def request_json(url, data=None, token=None):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        data = urllib.parse.urlencode(data).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=30) as response:
        return json.load(response)


def store_github_secrets(repository, credentials):
    values = {'SPOTIFY_CLIENT_ID': credentials['client_id'],
              'SPOTIFY_REFRESH_TOKEN': credentials['refresh_token'],
              'SPOTIFY_USER_ID': credentials['user_id']}
    for name, value in values.items():
        subprocess.run(['gh', 'secret', 'set', name, '--repo', repository], input=value.encode(),
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print('Stored GitHub Actions secret: ' + name, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--auth', type=Path, default=AUTH)
    parser.add_argument('--repository', default='nabekhan/liked-songs')
    parser.add_argument('--github-secrets', action='store_true')
    args = parser.parse_args()
    credentials = json.loads(args.auth.read_text())
    redirect = credentials.get('redirect_uri', 'http://127.0.0.1:8766/callback')
    parts = urllib.parse.urlsplit(redirect)
    if parts.hostname != '127.0.0.1' or parts.scheme != 'http':
        raise RuntimeError('Use an HTTP loopback callback registered with Spotify.')
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    state = secrets.token_urlsafe(32)
    outcome = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Callback URLs contain a single-use OAuth code.

        def do_GET(self):
            query = urllib.parse.urlsplit(self.path)
            values = urllib.parse.parse_qs(query.query)
            if query.path != parts.path or values.get('state', [''])[0] != state:
                self.send_error(400, 'Invalid OAuth callback')
                return
            try:
                if 'error' in values:
                    raise RuntimeError('Spotify authorization was declined.')
                token = request_json('https://accounts.spotify.com/api/token', {
                    'grant_type': 'authorization_code', 'client_id': credentials['client_id'],
                    'code': values['code'][0], 'redirect_uri': redirect, 'code_verifier': verifier})
                if not SCOPES <= set(token.get('scope', '').split()):
                    raise RuntimeError('Both library read and modify permissions are required.')
                profile = request_json('https://api.spotify.com/v1/me', token=token['access_token'])
                if credentials.get('user_id') and credentials['user_id'] != profile['id']:
                    raise RuntimeError('Spotify account differs from the configured target.')
                saved = {**credentials, **token, 'user_id': profile['id'],
                         'expires_at': time.time() + token['expires_in']}
                private_json(args.auth, saved)
                outcome['credentials'] = saved
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(b'<p>Spotify authorization complete. You can close this tab.</p>')
            except Exception as error:
                outcome['error'] = type(error).__name__
                self.send_error(400, 'Spotify authorization could not be completed')

    server = HTTPServer(('127.0.0.1', parts.port or 8766), Callback)
    server.timeout = 30
    url = 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode({
        'client_id': credentials['client_id'], 'response_type': 'code', 'redirect_uri': redirect,
        'scope': ' '.join(sorted(SCOPES)), 'state': state, 'code_challenge_method': 'S256',
        'code_challenge': challenge, 'show_dialog': 'true'})
    url_path = args.auth.parent / 'authorize-url.txt'
    with os.fdopen(os.open(url_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        stream.write(url)
    print('OAuth callback ready; authorization URL saved to ' + str(url_path), flush=True)
    deadline = time.monotonic() + 1800
    try:
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if 'credentials' not in outcome:
        raise RuntimeError('Spotify authorization did not complete successfully.')
    print('Spotify read/write authorization saved privately.', flush=True)
    if args.github_secrets:
        store_github_secrets(args.repository, outcome['credentials'])


if __name__ == '__main__':
    main()
