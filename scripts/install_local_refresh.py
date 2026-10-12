#!/usr/bin/env python3
"""Install a user LaunchAgent for the local GitHub mirror, without sudo."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'org.nabekhan.musicbrainz-liked-songs-refresh'
agent = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
logs = Path.home() / 'Library/Logs/musicbrainz-liked-songs'
agent.parent.mkdir(parents=True, exist_ok=True); logs.mkdir(parents=True, exist_ok=True)
config = {
    'Label': LABEL, 'ProgramArguments': [sys.executable, str(ROOT / 'scripts/refresh_local.py')],
    'WorkingDirectory': str(ROOT), 'RunAtLoad': True, 'StartInterval': 21600,
    'EnvironmentVariables': {'PATH': '/etc/profiles/per-user/nabeel/bin:/opt/homebrew/bin:/usr/bin:/bin'},
    'StandardOutPath': str(logs / 'refresh.log'), 'StandardErrorPath': str(logs / 'refresh-error.log'),
}
domain = 'gui/' + str(os.getuid())
if agent.exists(): subprocess.run(['launchctl', 'bootout', domain, str(agent)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
agent.write_bytes(plistlib.dumps(config)); os.chmod(agent, 0o644)
subprocess.run(['launchctl', 'bootstrap', domain, str(agent)], check=True)
print('Local refresh installed: every six hours and on login, while your Mac is available.')
