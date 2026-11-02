import json
import signal
from unittest.mock import patch

import pytest

from hermes_cli import maintenance


@pytest.mark.parametrize('role', ['gateway', 'dashboard'])
def test_release_can_arrive_after_five_minutes(tmp_path, role):
    request = {'attempt': 'synthetic', 'nonce': 'nonce', 'pair': 'fixture',
               'ports': {role: 0}, 'write_sets': {role: []}}
    calls = []

    class Server:
        def __init__(self, address, handler):
            assert address == ('127.0.0.1', 0)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def handle_request(self):
            calls.append(True)
            if len(calls) == 3:
                maintenance.write_once(tmp_path / 'release.json', request)
                maintenance.write_once(tmp_path / 'business-fence.json',
                                       dict(request, status='BUSINESS_MAY_HAVE_STARTED'))

    with patch.object(maintenance.http.server, 'HTTPServer', Server), \
            patch.object(maintenance.signal, 'signal'), \
            patch.object(maintenance.time, 'monotonic', side_effect=[0, 301, 302, 303]):
        maintenance.wait(role, tmp_path, request, {})

    ready = json.loads((tmp_path / (role + '.ready.json')).read_text())
    activated = json.loads((tmp_path / (role + '.activated.json')).read_text())
    assert len(calls) == 3
    assert ready['role'] == role
    assert ready['business_started'] is False
    assert activated['nonce'] == request['nonce']
    assert activated['status'] == 'activating_normal_entry'


@pytest.mark.parametrize('field,value', [('nonce', 'foreign'), ('attempt', 'foreign'),
                                       ('status', 'NOT_RELEASED')])
def test_release_still_requires_exact_durable_fence(tmp_path, field, value):
    request = {'attempt': 'synthetic', 'nonce': 'nonce', 'pair': 'fixture',
               'ports': {'gateway': 0}, 'write_sets': {'gateway': []}}
    maintenance.write_once(tmp_path / 'release.json', request)
    fence = dict(request, status='BUSINESS_MAY_HAVE_STARTED')
    fence[field] = value
    maintenance.write_once(tmp_path / 'business-fence.json', fence)
    with patch.object(maintenance.http.server, 'HTTPServer'), \
            patch.object(maintenance.signal, 'signal'):
        with pytest.raises(RuntimeError, match='matching durable fence'):
            maintenance.wait('gateway', tmp_path, request, {})
    assert not (tmp_path / 'gateway.activated.json').exists()


def test_stop_while_waiting_does_not_activate(tmp_path):
    request = {'attempt': 'synthetic', 'nonce': 'nonce', 'pair': 'fixture',
               'ports': {'dashboard': 0}, 'write_sets': {'dashboard': []}}
    handlers = {}

    def register(number, handler):
        handlers[number] = handler

    with patch.object(maintenance.http.server, 'HTTPServer') as server, \
            patch.object(maintenance.signal, 'signal', side_effect=register):
        server.return_value.__enter__.return_value.handle_request.side_effect = \
            lambda: handlers[signal.SIGTERM](signal.SIGTERM, None)
        with pytest.raises(SystemExit) as stopped:
            maintenance.wait('dashboard', tmp_path, request, {})
    assert stopped.value.code == 0
    assert not (tmp_path / 'dashboard.activated.json').exists()
