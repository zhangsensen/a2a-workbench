"""Manage this project's macOS LaunchAgent. Client configs are never edited."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import urllib.request

from settings import ROOT, BASE_URL

# Keep the legacy LaunchAgent label in v0.3.x so existing installations upgrade in place.
LABEL = 'io.github.a2a-roundtable'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
PUBLIC_SETTINGS = ('A2A_PORT', 'A2A_ROOM_DATA', 'A2A_CODEX_BIN', 'A2A_CLAUDE_BIN',
                   'A2A_ZCODE_BIN', 'A2A_ZCODE_CONFIG', 'A2A_CLAUDE_MODEL', 'A2A_ZCODE_MODEL', 'CODEX_HOME')


def unit():
    environment = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
                   'PYTHONUNBUFFERED': '1'}
    environment.update({key: os.environ[key] for key in PUBLIC_SETTINGS if key in os.environ})
    return {'Label': LABEL, 'ProgramArguments': [sys.executable, str(ROOT / 'roundtable.py')],
            'WorkingDirectory': str(ROOT), 'KeepAlive': True, 'RunAtLoad': True,
            'ThrottleInterval': 10, 'ExitTimeOut': 25, 'ProcessType': 'Background',
            'StandardOutPath': str(ROOT / 'logs/roundtable.log'),
            'StandardErrorPath': str(ROOT / 'logs/roundtable.log'),
            'EnvironmentVariables': environment}


def mcp_config():
    entry = {'command': sys.executable, 'args': [str(ROOT / 'roundtable_mcp.py')]}
    if 'A2A_PORT' in os.environ:
        entry['env'] = {'A2A_PORT': os.environ['A2A_PORT']}
    return {'mcpServers': {'patchcrew': entry}}


def launch(*args, check=True):
    return subprocess.run(['launchctl', *args], check=check, capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'status', 'restart', 'stop', 'start', 'mcp-config'])
    args = parser.parse_args()
    if args.action == 'mcp-config':
        print(json.dumps(mcp_config(), indent=2))
        return
    if args.action == 'status':
        with urllib.request.urlopen(BASE_URL + '/healthz', timeout=5) as response:
            print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
        return
    if sys.platform != 'darwin':
        parser.error('LaunchAgent management requires macOS; run roundtable.py directly on other Unix hosts')
    domain = f'gui/{os.getuid()}'
    target = f'{domain}/{LABEL}'
    if args.action == 'install':
        if launch('print', target, check=False).returncode == 0:
            raise SystemExit('Already installed and running. Stop this service before reinstalling its configuration.')
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        (ROOT / 'logs').mkdir(exist_ok=True)
        temporary = PLIST.with_suffix('.plist.tmp')
        with open(temporary, 'wb') as output:
            os.chmod(temporary, 0o600)
            plistlib.dump(unit(), output)
        os.replace(temporary, PLIST)
        launch('enable', target)
        launch('bootstrap', domain, str(PLIST))
        print('Installed', LABEL, 'at', BASE_URL)
        print('Client settings were not changed. Use service.py mcp-config to print connection settings.')
    elif args.action == 'restart':
        launch('kill', 'SIGTERM', target)
        print('Graceful restart requested; persisted room sessions are retained.')
    elif args.action == 'stop':
        launch('bootout', target)
    elif args.action == 'start':
        launch('enable', target)
        launch('bootstrap', domain, str(PLIST))


if __name__ == '__main__':
    main()
