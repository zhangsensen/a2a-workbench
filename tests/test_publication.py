import importlib.util
from pathlib import Path

import pytest

from room_agents import executable, clean_env
from runtime_rpc import RuntimeFailure
import service

spec = importlib.util.spec_from_file_location('publication_guard', Path(__file__).parents[1] / 'scripts/check_publication.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def test_publication_rejects_private_state_and_values():
    for name in ('data/rooms.sqlite3','logs/service.log','.env','account.key'):
        assert guard.inspect(name,b'private')
    assert guard.inspect('settings.py', ('path = ' + '/Users/' + 'sample-person/config.json').encode())
    assert guard.inspect('settings.py', ('token = ' + 'ghp_' + 'x'*40).encode())
    assert guard.inspect('settings.py', ('"apiKey": "' + 'x'*32 + '"').encode())
    assert guard.inspect('attachment.bin',b'\xff\x00')
    assert guard.inspect('.env.example',b'A2A_PORT=41241\n') == []
    assert guard.inspect('safe.py',b'config.get("apiKey")') == []


def test_official_cli_resolution_is_portable(tmp_path,monkeypatch):
    binary=tmp_path/'claude'
    binary.write_text('#!/bin/sh\nexit 0\n');binary.chmod(0o700)
    monkeypatch.setenv('A2A_CLAUDE_BIN',str(binary))
    assert executable('claude')==str(binary)
    monkeypatch.setenv('A2A_CLAUDE_BIN',str(tmp_path/'missing'))
    with pytest.raises(RuntimeFailure):executable('claude')


def test_launchagent_does_not_export_unrelated_secrets(monkeypatch):
    monkeypatch.setenv('UNRELATED_SECRET','do-not-export')
    monkeypatch.setenv('ANTHROPIC_API_KEY','do-not-export')
    monkeypatch.setenv('A2A_PORT','41243')
    result=service.unit()
    assert result['Label']=='io.github.a2a-roundtable'
    assert result['EnvironmentVariables']['A2A_PORT']=='41243'
    assert 'UNRELATED_SECRET' not in result['EnvironmentVariables']
    assert 'ANTHROPIC_API_KEY' not in result['EnvironmentVariables']
    assert 'bootout' not in result['ProgramArguments']


def test_mcp_configuration_is_print_only_and_port_aware(monkeypatch,tmp_path):
    monkeypatch.setenv('A2A_PORT','41243')
    monkeypatch.setattr(service,'ROOT',tmp_path)
    before=list(tmp_path.iterdir())
    entry=service.mcp_config()['mcpServers']['a2a-workbench']
    assert entry['env']=={'A2A_PORT':'41243'}
    assert entry['args']==[str(tmp_path/'roundtable_mcp.py')]
    assert list(tmp_path.iterdir())==before


def test_claude_environment_uses_existing_login(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY','synthetic')
    monkeypatch.setenv('CLAUDE_CODE_TEST','synthetic')
    assert not any(k.startswith(('ANTHROPIC_','CLAUDE_')) for k in clean_env())
