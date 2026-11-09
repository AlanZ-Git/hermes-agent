import os
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent.recovery_lifecycle import RecoveryLifecycle, request_thread, request_target, register_request, finish_request

_IDENTITIES = {k: v for k, v in os.environ.items() if k.startswith(('HERMES_SESSION_', 'HERMES_DELEGATED_', 'HERMES_WORKER_')) or k == 'HERMES_GATEWAY_SESSION'}


@pytest.fixture(autouse=True)
def retained_identity(_hermetic_environment, monkeypatch):
    for key, value in _IDENTITIES.items():
        monkeypatch.setenv(key, value)


def test_nonstream_timeout_retains_actual_request_until_outer_cleanup(monkeypatch):
    from agent import chat_completion_nonstream as n
    entered, release = Event(), Event()
    agent = SimpleNamespace(api_mode='chat_completions', _interrupt_requested=False, _touch_activity=lambda _: None)
    wd = SimpleNamespace(codex=False, idle_requires_progress=False, ttfb_enabled=False, idle_enabled=False,
                         progress_timeout=0, stale_timeout=-1)
    monkeypatch.setattr(n.h, '_resolve_nonstream_watchdogs', lambda *args: wd)
    monkeypatch.setattr(n.h, '_dispatch_nonstreaming_api_request', lambda *args, **kwargs: (entered.set(), release.wait(10), 'late response')[2])
    request = n._NonStreamRequest(agent, {'model': 'local-fixture'})
    def timeout(_elapsed):
        request.result['error'] = TimeoutError('synthetic watchdog')
    monkeypatch.setattr(request, '_stale_kill', timeout)
    def owner():
        try:
            with tracker.scope():
                with pytest.raises(TimeoutError):
                    request.run()
        finally:
            tracker.finish_owner(True)
    thread = Thread(target=owner)
    tracker = RecoveryLifecycle(thread)
    try:
        thread.start()
        assert entered.wait(10)
        thread.join(10)
        assert not thread.is_alive()
        assert tracker.observe()['outcome'] == 'running'
    finally:
        release.set()
        thread.join(10)
        if request.thread:
            request.thread.join(10)
    assert tracker.observe() == {'outcome': 'exited_proven', 'reason': 'owner_and_registered_requests_exited', 'request_count': 1}


def test_retries_keep_all_exact_request_threads():
    entered, release = Event(), Event()
    children = []
    def owner():
        with tracker.scope():
            old = request_thread(object(), lambda: (entered.set(), release.wait(10)), daemon=True)
            children.append(old)
            old.start()
            assert entered.wait(10)
            retry = request_thread(object(), lambda: 'second attempt', daemon=True)
            children.append(retry)
            retry.start()
            retry.join(10)
        tracker.finish_owner(True)
    thread = Thread(target=owner)
    tracker = RecoveryLifecycle(thread)
    try:
        thread.start()
        assert entered.wait(10)
        thread.join(10)
        assert tracker.observe()['outcome'] == 'running'
    finally:
        release.set()
        thread.join(10)
        for child in children:
            child.join(10)
    assert tracker.observe()['outcome'] == 'exited_proven'
    assert tracker.observe()['request_count'] == 2


def test_inline_registration_and_owner_cleanup_are_both_required():
    def owner():
        with tracker.scope():
            request_target(object(), lambda: None)()
            registration = register_request(object())
            finish_request(registration)
        tracker.finish_owner(False)
    thread = Thread(target=owner)
    tracker = RecoveryLifecycle(thread)
    thread.start()
    thread.join(10)
    assert tracker.observe()['outcome'] == 'unknown'
    tracker.finish_owner(True)
    assert tracker.observe()['outcome'] == 'exited_proven'
    assert tracker.observe()['request_count'] == 2


def test_unsupported_provider_is_not_zero_request_exit_proof():
    thread = Thread(target=lambda: None)
    tracker = RecoveryLifecycle(thread)
    thread.start()
    thread.join(10)
    tracker.finish_owner(True)
    tracker.unsupported('external process not owned')
    assert tracker.observe()['outcome'] == 'unknown'


@pytest.mark.parametrize('platform', ['cli', 'subagent'])
def test_actual_local_streaming_provider_exit_and_pool_reuse(platform):
    from agent.chat_completion_helpers import interruptible_streaming_api_call
    from tests.agent.test_direct_contexts_stream_inline import _Wire, _make_agent, _KW
    wire = _Wire()
    responses, errors = [], []
    agent = _make_agent(wire.base_url, platform=platform)
    def owner():
        try:
            with tracker.scope():
                for _ in range(2):
                    responses.append(interruptible_streaming_api_call(agent, dict(_KW)))
        except BaseException as error:
            errors.append(error)
        finally:
            tracker.finish_owner(not errors)
    thread = Thread(target=owner)
    tracker = RecoveryLifecycle(thread)
    try:
        thread.start()
        thread.join(20)
        assert not thread.is_alive()
        assert not errors, errors
        assert [response.choices[0].message.content for response in responses] == ['hello world', 'hello world']
        assert tracker.observe()['outcome'] == 'exited_proven'
        assert tracker.observe()['request_count'] == 2
        assert len([request for request in wire.requests if 'messages' in request]) == 2
    finally:
        wire.close()


@pytest.mark.parametrize('inline', [False, True])
def test_actual_nonstream_provider_and_direct_inline_cleanup(inline):
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from agent.chat_completion_helpers import direct_api_call, interruptible_api_call
    from tests.agent.test_direct_contexts_stream_inline import _make_agent, _KW
    calls, responses, errors = [], [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('content-length', 0))) or b'{}')
            if not self.path.endswith('/chat/completions'):
                self.send_response(404)
                self.end_headers()
                return
            calls.append(body)
            raw = json.dumps({'id': 'local', 'object': 'chat.completion', 'created': 1, 'model': 'm',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'local reply'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}).encode()
            self.send_response(200)
            self.send_header('content-type', 'application/json')
            self.send_header('content-length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    agent = _make_agent(f'http://127.0.0.1:{server.server_address[1]}/v1', platform='subagent' if inline else 'cli')
    def owner():
        try:
            with tracker.scope():
                call = direct_api_call if inline else interruptible_api_call
                for _ in range(2):
                    responses.append(call(agent, dict(_KW)))
        except BaseException as error:
            errors.append(error)
        finally:
            tracker.finish_owner(not errors)
    thread = Thread(target=owner)
    tracker = RecoveryLifecycle(thread)
    try:
        thread.start()
        thread.join(20)
        assert not thread.is_alive()
        assert not errors, errors
        assert [response.choices[0].message.content for response in responses] == ['local reply', 'local reply']
        assert tracker.observe()['outcome'] == 'exited_proven'
        assert tracker.observe()['request_count'] == 2
        assert len(calls) == 2 and all(not call.get('stream') for call in calls)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(10)


def test_normal_requests_have_no_recovery_scope():
    assert register_request(object()) is None
    target = lambda: 'normal response'
    assert request_target(object(), target) is target
    assert request_target(object(), target)() == 'normal response'
