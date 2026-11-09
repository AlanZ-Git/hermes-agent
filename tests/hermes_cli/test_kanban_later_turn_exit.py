"""Later goal turns reach process exit and durable dispatcher accounting."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if not body.get('messages'):
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.server.requests.append(body)
        second = any(m.get('role') == 'user' and str(m.get('content', '')).startswith(
            '[Continuing toward this kanban task') for m in body['messages'])
        outcome = self.server.outcome
        code = {'quota': 429, 'billing': 402, 'ordinary': 400}.get(outcome, 200) if second else 200
        if code != 200:
            payload = {'error': {'message': {'quota': 'quota exceeded', 'billing': 'insufficient credits',
                                           'ordinary': 'synthetic invalid request'}[outcome],
                                 'type': 'insufficient_quota' if outcome == 'quota' else 'invalid_request_error',
                                 'code': 'insufficient_quota' if outcome == 'quota' else str(code)}}
            data = json.dumps(payload).encode()
            kind = 'application/json'
        else:
            message = {'role': 'assistant', 'content': 'first step' if not second else 'finished'}
            payload = {'id': 'synthetic', 'model': 'local-only', 'object': 'chat.completion',
                       'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}
            data, kind = json.dumps(payload).encode(), 'application/json'
            if body.get('stream'):
                data = ('data: ' + json.dumps({'id': 'synthetic', 'choices': [
                    {'index': 0, 'delta': message, 'finish_reason': 'stop'}]}) + '\n\ndata: [DONE]\n\n').encode()
                kind = 'text/event-stream'
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def child(params):
    import socket
    original_connect = socket.socket.connect
    def loopback_only(sock, address):
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise AssertionError('synthetic provider must remain loopback-only')
        return original_connect(sock, address)
    socket.socket.connect = loopback_only
    from hermes_cli import goals, kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_state import SessionDB
    from run_agent import AIAgent
    root = Path(__file__).resolve().parents[2]
    assert Path(kb.__file__).resolve().is_relative_to(root)
    assert Path(params['board']).resolve().is_relative_to(Path(os.environ['TMPDIR']).resolve())
    judges = []
    def judge(*args, **kwargs):
        judges.append(1)
        assert len(judges) <= 2, 'extra judge after failed turn'
        return ('continue' if len(judges) == 1 else 'done', 'synthetic judge', False, None, False)
    goals.judge_goal = judge
    db = SessionDB(db_path=Path(params['home']) / 'state.db')
    agent = AIAgent(base_url=params['url'], api_key='synthetic-loopback-only', provider='custom',
                    api_mode='chat_completions', model='local-only', enabled_toolsets=[],
                    quiet_mode=True, session_id='worker-chat', session_db=db,
                    skip_context_files=True, skip_memory=True, skip_background_review=True,
                    cwd=params['home'], max_iterations=8)
    os.environ.update(HERMES_KANBAN_TASK=params['tid'], HERMES_KANBAN_RUN_ID=str(params['rid']),
                      HERMES_KANBAN_DB=params['board'], HERMES_KANBAN_GOAL_MODE='1')
    try:
        if params['surface'] == 'managed':
            from gateway.session_kanban import _run_worker_turns
            from hermes_cli.turn_exit import turn_exit_code
            with connect_closing(Path(params['board'])) as conn, kb.write_txn(conn):
                kb._append_event(conn, params['tid'], 'worker_bound',
                                 {'pid': os.getpid(), 'claim_lock': params['claim']}, run_id=params['rid'])
            context = {'db': params['board'], 'task_id': params['tid'], 'run_id': params['rid'],
                       'claim_lock': params['claim'], 'skills': [], 'context': '', 'goal_mode': True,
                       'goal_text': 'finish two steps', 'goal_max_turns': 2}
            result = _run_worker_turns(agent, {'policy': {'kanban_json': json.dumps(context)}, 'text': 'step one'}, [])
            code = turn_exit_code(result, kanban_worker=True)
            raise SystemExit(code)
        else:
            import cli
            from hermes_cli.cli_single_query import _run_quiet_single_query
            from hermes_cli import quiet_single_query
            quiet_single_query.continue_quiet_notify_completions = lambda *a, **kw: None
            cli_obj = SimpleNamespace(agent=agent, session_id=agent.session_id, conversation_history=[])
            _run_quiet_single_query(cli_obj, 'step one')
    finally:
        Path(params['home'], 'judge-count.json').write_text(json.dumps(len(judges)))
        agent.close()
        db.close()


@pytest.mark.parametrize('surface', ['managed', 'quiet'])
@pytest.mark.parametrize('outcome', ['quota', 'billing', 'budget', 'ordinary'])
def test_later_turn_process_exit_and_durable_cooldown(tmp_path, monkeypatch, surface, outcome):
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    from hermes_cli.kanban_db_connect import connect_closing
    assert tmp_path.resolve().is_relative_to(Path(os.environ['TMPDIR']).resolve())
    home = tmp_path / 'home'
    home.mkdir()
    board = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(board))
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    monkeypatch.setenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS', '300')
    with connect_closing(board) as conn:
        tid = kb.create_task(conn, title='finish two steps', assignee='synthetic', goal_mode=True, goal_max_turns=2)
        task = kb.claim_task(conn, tid)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    server.requests, server.outcome = [], outcome
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'model': {'provider': 'custom', 'default': 'local-only', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}},
        'platform_toolsets': {'cli': []}, 'terminal': {'cwd': str(home)}}))
    params = dict(surface=surface, outcome=outcome, url=url, home=str(home), board=str(board),
                  tid=tid, rid=task.current_run_id, claim=task.claim_lock)
    env = dict(os.environ, HOME=str(home), HERMES_HOME=str(home), HERMES_KANBAN_DB=str(board),
               PYTHONDONTWRITEBYTECODE='1', HERMES_QUIET_NOTIFY_LINGER_SECONDS='0')
    code = 'import runpy,sys,json; m=runpy.run_path(sys.argv[1]); m["child"](json.loads(sys.argv[2]))'
    process = None
    try:
        with (home / 'worker.log').open('w+') as log:
            process = subprocess.Popen([sys.executable, '-B', '-c', code, str(Path(__file__).resolve()), json.dumps(params)],
                                       env=env, stdout=log, stderr=subprocess.STDOUT)
            dispatch._live_worker_procs[process.pid] = process
            with connect_closing(board) as conn:
                dispatch._set_worker_pid(conn, tid, process.pid, run_id=task.current_run_id, claim_lock=task.claim_lock)
            rc = process.wait(timeout=90)
            log.seek(0)
            output = log.read()
        expected = 75 if outcome in ('quota', 'billing') else 0 if outcome == 'budget' else 1
        assert rc == expected, output
        assert len(server.requests) == 4, output
        assert json.loads((home / 'judge-count.json').read_text()) == (2 if outcome == 'budget' else 1), output
        with connect_closing(board) as conn:
            if surface == 'managed':
                recorded = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_result'",
                                        (tid, task.current_run_id)).fetchone()
                assert json.loads(recorded[0])['exit_code'] == expected
                if outcome != 'budget':
                    from hermes_cli.kanban_owner_recovery import managed_run_exit_code
                    assert managed_run_exit_code(conn, tid, process.pid, task.claim_lock) == expected
            if outcome == 'budget':
                assert kb.get_task(conn, tid).status == 'blocked'
                assert dispatch.check_respawn_guard(conn, tid) != 'rate_limit_cooldown'
            if outcome in ('quota', 'billing', 'ordinary'):
                dispatch._record_worker_exit(process.pid, dispatch._wait_status_from_returncode(
                    1 if surface == 'managed' else rc))
                sweep = dispatch._reclaim_dead_workers(conn)
                dispatch._account_crashes(conn, sweep.crash_details)
                saved = kb.get_task(conn, tid)
                run = conn.execute('SELECT * FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()
                assert saved.status == 'ready'
                if expected == 75:
                    assert run['outcome'] == 'rate_limited'
                    assert saved.consecutive_failures == 0
                    assert dispatch.check_respawn_guard(conn, tid) == 'rate_limit_cooldown'
                else:
                    assert run['outcome'] == 'crashed'
                    assert saved.consecutive_failures == 1
                    assert dispatch.check_respawn_guard(conn, tid) != 'rate_limit_cooldown'
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            dispatch._live_worker_procs.pop(process.pid, None)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
