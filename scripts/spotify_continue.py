#!/usr/bin/env python3
"""Resume one backed-up Spotify reconciliation after an API cooldown.

This is a one-time job, not a weekly sync. Its LaunchAgent removes itself after
verified completion or a failure requiring review. Private run data stays local.
"""
import argparse
import datetime as dt
import fcntl
import os
from pathlib import Path
import plistlib
import subprocess
import sys

from spotify_authorize import store_github_secrets
from spotify_sync import AUTH, ROOT, Spotify, SpotifyCooldown, apply_plan, make_plan, read_json, save_json

LABEL = 'org.nabekhan.liked-songs-spotify-once'
AGENT = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')


def status(run_dir, state, **details):
    save_json(run_dir / 'continuation-status.json', {
        'state': state, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(), **details})


def continue_run(api, run_dir, source):
    result_path = run_dir / 'result.json'
    if result_path.exists():
        result = read_json(result_path)
        if result.get('missing_desired_tracks') != 0 or result.get('extra_tracks') != 0:
            raise RuntimeError('Existing completion record is inconsistent; review this run.')
        status(run_dir, 'complete', result=result)
        return 'complete'
    try:
        api.check_cooldown()
        if not (run_dir / 'changes.json').exists():
            status(run_dir, 'matching')
            make_plan(api, run_dir, source, resolve=True)
        status(run_dir, 'applying')
        result = apply_plan(api, run_dir)
        status(run_dir, 'complete', result=result)
        return 'complete'
    except SpotifyCooldown as error:
        status(run_dir, 'waiting_for_spotify',
               retry_at=dt.datetime.fromtimestamp(error.retry_at, dt.timezone.utc).isoformat())
        print(str(error), flush=True)
        return 'waiting'


def install(args):
    if AGENT.exists():
        raise RuntimeError('A one-time Spotify continuation is already installed.')
    if not (args.run_dir / 'spotify-before.json.gz').exists():
        raise RuntimeError('A complete backup is required before scheduling this run.')
    args.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    arguments = [sys.executable, str(Path(__file__).resolve()), '--run-dir', str(args.run_dir),
                 '--source', str(args.source), '--auth', str(args.auth), '--agent', str(AGENT)]
    if args.github_repository:
        arguments.extend(['--github-repository', args.github_repository])
    config = {'Label': LABEL, 'ProgramArguments': arguments, 'WorkingDirectory': str(ROOT),
              'RunAtLoad': True, 'StartInterval': 900, 'ProcessType': 'Background',
              'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONUNBUFFERED': '1'},
              'StandardOutPath': str(args.run_dir / 'continuation.log'),
              'StandardErrorPath': str(args.run_dir / 'continuation-error.log')}
    for filename in ('continuation.log', 'continuation-error.log'):
        descriptor = os.open(args.run_dir / filename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(descriptor)
    AGENT.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(AGENT, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(plistlib.dumps(config))
    try:
        subprocess.run(['launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(AGENT)], check=True)
    except Exception:
        AGENT.unlink(missing_ok=True)
        raise
    print('Installed one-time Spotify continuation. It respects the saved cooldown and stops after this run.')


def retire(agent, run_dir):
    if agent != AGENT or not agent.exists():
        return
    config = plistlib.loads(agent.read_bytes())
    arguments = config.get('ProgramArguments', [])
    if config.get('Label') != LABEL or str(run_dir) not in arguments or str(Path(__file__).resolve()) not in arguments:
        raise RuntimeError('The continuation LaunchAgent does not belong to this run.')
    agent.unlink()
    print('One-time Spotify continuation retired.', flush=True)
    subprocess.run(['launchctl', 'bootout', 'gui/' + str(os.getuid()) + '/' + LABEL], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--auth', type=Path, default=AUTH)
    parser.add_argument('--github-repository', help='Update the repository secrets if a refresh token rotates.')
    parser.add_argument('--install', action='store_true', help='Install a one-time macOS LaunchAgent.')
    parser.add_argument('--agent', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.run_dir, args.source, args.auth = args.run_dir.resolve(), args.source.resolve(), args.auth.resolve()
    if args.install:
        install(args)
        return
    args.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = args.run_dir / 'continuation.lock'
    with os.fdopen(os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600), 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # Another invocation owns this run.
        api, state = Spotify(args.auth), 'blocked'
        old_refresh = api.auth['refresh_token']
        try:
            state = continue_run(api, args.run_dir, args.source)
        except Exception as error:
            # The controlled RuntimeError messages contain no OAuth values or URLs.
            message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            status(args.run_dir, 'blocked', reason=message)
            print('Spotify continuation stopped: ' + message, file=sys.stderr, flush=True)
        finally:
            if args.github_repository and api.auth['refresh_token'] != old_refresh:
                try:
                    store_github_secrets(args.github_repository, api.auth)
                except subprocess.SubprocessError:
                    state = 'blocked'
                    status(args.run_dir, 'blocked', reason='Updating the rotated GitHub refresh-token secret failed.')
                    print('GitHub secret update failed; the current token is preserved in the private auth file.', file=sys.stderr)
            if state != 'waiting' and args.agent:
                retire(args.agent, args.run_dir)


if __name__ == '__main__':
    main()
