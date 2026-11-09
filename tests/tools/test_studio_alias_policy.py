import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

ALIASES = ('ekko_studio_api_request', 'hermes_studio_api_request', 'hermes_api_request')


def test_alias_guard_precedes_any_transport(monkeypatch):
    from agent.delegation_context import delegated_child_context
    from tools import mcp_tool_handlers as handlers
    monkeypatch.setattr(handlers, '_trust_gate_check', lambda *a: pytest.fail('denial reached transport admission'))
    for alias in ALIASES:
        handler = handlers._make_tool_handler('synthetic', alias, 5)
        for path in ('/api/hermes/kanban', ' /api/hermes/kanban/complete ',
                     '/api/hermes/x/../kanban/complete', '/api/hermes/%6banban/complete',
                     r'\api\hermes\kanban\complete'):
            with delegated_child_context():
                assert 'refused for delegated children' in handler({'method': 'POST', 'path': path})


def test_studio_list_registration_to_local_http(tmp_path, monkeypatch):
    script = os.getenv('TODO228_STUDIO_MCP')
    if not script:
        pytest.skip('set TODO228_STUDIO_MCP to the approved Studio MCP source')
    assert Path(script).is_file()
    from agent.delegation_context import delegated_child_context
    from tools.registry import registry
    from tools.mcp_tool_discovery import register_mcp_servers
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools import mcp_tool as core
    from tools import mcp_tool_registration as registration
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(('GET', self.path))
            if self.path.startswith('/api/openapi.json'):
                response = {'openapi': '3.0.0', 'info': {'title': 'synthetic', 'version': '1'}, 'paths': {
                    '/api/hermes/kanban/complete': {'post': {'responses': {'200': {'description': 'ok'}}}},
                    '/api/ordinary': {'get': {'responses': {'200': {'description': 'ok'}}}}}}
            else:
                response = {'ok': True, 'synthetic': True}
            self.reply(response)

        def do_POST(self):
            calls.append(('POST', self.path))
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.reply({'ok': True, 'synthetic': True})

        def reply(self, value):
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever)
    thread.start()
    name = 'policy_studio_fixture'
    import shutil
    node = shutil.which('node')
    assert node
    import base64
    import time
    def b64(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    token = b64({'alg': 'HS256'}) + '.' + b64({'type': 'access', 'aud': 'hermes-studio',
            'sub': 'synthetic', 'username': 'synthetic', 'role': 'admin', 'exp': int(time.time()) + 3600}) + '.synthetic'
    config = {'command': node, 'args': [script, 'api'], 'connect_timeout': 15, 'timeout': 10,
              'trust': 'full', 'env': {'HERMES_WEB_UI_URL': f'http://127.0.0.1:{http.server_port}',
                                        'HERMES_WEB_UI_HOME': str(tmp_path / 'studio'),
                                        'HERMES_WEB_UI_TOKEN': token,
                                        'HOME': str(tmp_path)}}
    try:
        registered = register_mcp_servers({name: config})
        canonical = f'mcp__{name}__ekko_studio_api_request'
        assert canonical in registered, registered
        server = next(s for s in core._servers.values() if s.name == name)
        listed = next(t for t in server._tools if t.name == ALIASES[0])
        # Studio lists the canonical name; its accepted legacy aliases use the same schema.
        server._tools.extend(listed.model_copy(update={'name': alias}) for alias in ALIASES[1:])
        registration._register_server_tools(name, server, config)
        for alias in ALIASES:
            entry = registry.get_entry(f'mcp__{name}__{alias}')
            assert entry is not None
            before = list(calls)
            with delegated_child_context():
                refused = registry.dispatch(f'mcp__{name}__{alias}', {'method': 'POST', 'path': '/api/hermes/kanban/complete'})
            assert 'refused for delegated children' in refused
            assert calls == before
            with delegated_child_context():
                normal = registry.dispatch(f'mcp__{name}__{alias}', {'method': 'GET', 'path': '/api/ordinary'})
            assert 'synthetic' in normal, normal
            assert any(method == 'GET' and path.startswith('/api/ordinary') for method, path in calls)
        # A synthetic parent identity control does not remove the inherited worker fence.
        with monkeypatch.context() as parent:
            parent.setattr('agent.delegation_context.is_delegated_child_process_context', lambda: False)
            for alias in ALIASES:
                normal = registry.dispatch(f'mcp__{name}__{alias}', {'method': 'POST', 'path': '/api/hermes/kanban/complete'})
                assert 'synthetic' in normal, normal
        assert sum(method == 'POST' for method, _ in calls) == 3
    finally:
        shutdown_mcp_servers()
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
