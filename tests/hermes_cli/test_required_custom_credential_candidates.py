"""Declared credentials fail only after all legal final candidates are exhausted."""
import json
import os
from pathlib import Path
import shlex
import sys
import threading
from http.server import ThreadingHTTPServer

from openai import OpenAI
import pytest

from hermes_cli import runtime_provider as rp
from tests.hermes_cli.test_kanban_later_turn_exit import Provider


@pytest.mark.parametrize('route', ['named', 'direct'])
@pytest.mark.parametrize('candidate', ['missing', 'explicit', 'pool', 'inline', 'env', 'key_cmd', 'keyless'])
def test_final_candidate_priority_and_zero_request_failfast(tmp_path, monkeypatch, route, candidate):
    assert tmp_path.resolve().is_relative_to(Path(os.environ['TMPDIR']).resolve())
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('TODO228_SYNTHETIC_KEY', 'synthetic-env-key' if candidate == 'env' else '')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-foreign-host-key')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    server.requests, server.outcome = [], 'budget'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/v1'
    entry = {'api': url, 'default_model': 'local-only', 'key_env': 'TODO228_SYNTHETIC_KEY'}
    if candidate == 'inline':
        entry['api_key'] = 'synthetic-inline-key'
    if candidate == 'key_cmd':
        command = home / 'key.py'
        command.write_text("print('synthetic-command-key')\n")
        entry['key_cmd'] = f'{shlex.quote(sys.executable)} {shlex.quote(str(command))}'
    if candidate == 'keyless':
        entry.pop('key_env')
    config = {'model': {'provider': 'custom:todo228', 'base_url': url, 'default': 'local-only'},
              'providers': {'todo228': entry}}
    (home / 'config.yaml').write_text(json.dumps(config))
    pool = {} if candidate != 'pool' else {'todo228': [
        {'id': 'synthetic', 'label': 'synthetic', 'source': 'manual', 'auth_type': 'api_key',
         'priority': 0, 'access_token': 'synthetic-pool-key', 'base_url': url}]}
    (home / 'auth.json').write_text(json.dumps({'version': 1, 'providers': {}, 'credential_pool': pool}))
    monkeypatch.setattr(rp, 'load_config', lambda: config)
    kwargs = {'requested': 'custom:todo228'} if route == 'named' else {'requested': 'custom', 'explicit_base_url': url}
    if candidate == 'explicit':
        kwargs['explicit_api_key'] = 'synthetic-explicit-key'
    try:
        with rp.frozen_runtime_config(config):
            if candidate == 'missing':
                with pytest.raises(rp.AuthError) as exc:
                    rp.resolve_runtime_provider(**kwargs)
                assert exc.value.code == 'auth_unavailable'
                assert not server.requests
                return
            runtime = rp.resolve_runtime_provider(**kwargs)
        key = runtime['api_key']() if callable(runtime['api_key']) else runtime['api_key']
        expected = {'explicit': 'synthetic-explicit-key', 'pool': 'synthetic-pool-key',
                    'inline': 'synthetic-inline-key', 'env': 'synthetic-env-key',
                    'key_cmd': 'synthetic-command-key', 'keyless': 'no-key-required'}[candidate]
        assert key == expected
        with OpenAI(base_url=runtime['base_url'], api_key=key, max_retries=0, timeout=5) as client:
            result = client.chat.completions.create(model='local-only', messages=[{'role': 'user', 'content': 'control'}])
        assert result.choices[0].message.content == 'first step'
        assert len(server.requests) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
