"""External blocks retain the original execution until exact exit proof."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as connect
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_termination as termination


@pytest.fixture
def board(tmp_path, monkeypatch):
    path = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    assert path.resolve().is_relative_to(Path(os.environ['TMPDIR']).resolve())
    with connect.connect(db_path=path) as conn:
        yield conn


def claimed(conn):
    tid = kb.create_task(conn, title='isolated execution', assignee='synthetic')
    task = kb.claim_task(conn, tid)
    assert task is not None
    return tid, task


@pytest.fixture
def worker():
    process = subprocess.Popen([sys.executable, '-c', 'import threading; threading.Event().wait(120)'])
    dispatch._live_worker_procs[process.pid] = process
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=8)
        dispatch._live_worker_procs.pop(process.pid, None)


@pytest.mark.platforms('linux')
def test_external_block_stops_owned_worker_before_replacement(board, worker):
    tid, task = claimed(board)
    dispatch._set_worker_pid(board, tid, worker.pid, run_id=task.current_run_id, claim_lock=task.claim_lock)
    assert kb.block_task(board, tid, reason='external dependency', kind='needs_input') is True
    worker.wait(timeout=8)
    assert worker.returncode != 0
    assert not termination.termination_pending(board, tid)
    saved = kb.get_task(board, tid)
    assert saved.status == 'blocked' and saved.claim_lock is None
    assert kb.unblock_task(board, tid)
    replacement = kb.claim_task(board, tid)
    assert replacement.current_run_id != task.current_run_id


@pytest.mark.platforms('linux')
def test_signal_denied_preserves_original_claim_and_blocks_all_releases(board, worker, monkeypatch):
    tid, task = claimed(board)
    dispatch._set_worker_pid(board, tid, worker.pid, run_id=task.current_run_id, claim_lock=task.claim_lock)
    def denied(*args):
        raise PermissionError('synthetic signal refusal')
    monkeypatch.setattr(signal, 'pidfd_send_signal', denied)
    assert kb.block_task(board, tid, reason='external dependency', kind='needs_input') is False
    assert worker.poll() is None
    saved = kb.get_task(board, tid)
    assert saved.current_run_id == task.current_run_id and saved.claim_lock == task.claim_lock
    request = termination.pending_requests(board, tid)[0]
    assert not kb.block_task(board, tid, reason='retry')
    assert termination.pending_requests(board, tid)[0]['id'] == request['id']
    assert not kb.complete_task(board, tid, result='late', expected_run_id=task.current_run_id)
    assert not kb.unblock_task(board, tid)
    assert not kb.reclaim_task(board, tid)
    assert not kb.schedule_task(board, tid)
    assert not kb.delete_task(board, tid)
    assert dispatch._record_task_failure(board, tid, 'late failure', outcome='crashed') is False
    assert kb.get_task(board, tid).consecutive_failures == saved.consecutive_failures


def test_task_wide_hold_survives_current_run_loss_and_malformed_settlement(board):
    tid, task = claimed(board)
    request_id = termination.request_block(board, tid, reason='dependency', kind='needs_input')
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='ready',current_run_id=NULL,claim_lock=NULL WHERE id=?", (tid,))
        kb._append_event(board, tid, 'block_termination_settled', {'request_event_id': request_id,
            'run_id': task.current_run_id + 1, 'claim_lock': task.claim_lock, 'exit_proven': True,
            'workers': [], 'outcome': 'blocked'}, run_id=task.current_run_id + 1)
    assert termination.termination_pending(board, tid)
    assert kb.claim_task(board, tid) is None
    assert kb.claim_review_task(board, tid) is None
    assert not kb.delete_task(board, tid)


def test_invalid_request_payload_remains_unknown(board):
    tid, task = claimed(board)
    with kb.write_txn(board):
        event = kb._append_event(board, tid, 'block_termination_requested', {'workers': 'invalid'}, run_id=task.current_run_id)
        board.execute("UPDATE task_events SET payload='[' WHERE id=?", (event,))
    assert termination.termination_pending(board, tid)
    assert not termination.settle_request(board, tid, event)
    assert kb.claim_task(board, tid) is None


def test_claim_before_spawn_can_be_proven_undispatched_and_self_block_keeps_waiting(board):
    tid, task = claimed(board)
    assert kb.block_task(board, tid, reason='external before spawn', kind='needs_input')
    for _ in range(4):
        assert kb.unblock_task(board, tid)
        current = kb.claim_task(board, tid)
        assert kb.block_task(board, tid, reason='human decision', kind='needs_input', expected_run_id=current.current_run_id)
        assert kb.get_task(board, tid).status == 'blocked'
    assert kb.get_task(board, tid).block_recurrences >= kb.BLOCK_RECURRENCE_LIMIT


@pytest.mark.platforms('linux')
def test_untrusted_absent_pid_is_not_exit_proof(board):
    tid, task = claimed(board)
    assert termination.observe_worker({'pid': 2147483647, 'started_at': 'invented'}, task.claim_lock) == 'UNKNOWN'


@pytest.mark.parametrize('entry', ['dashboard', 'descendants', 'review', 'changes', 'promote', 'reopen', 'adopt'])
def test_remaining_lifecycle_entrypoints_preserve_pending_execution(board, entry):
    tid, task = claimed(board)
    request = termination.request_block(board, tid, reason='committed before stop', kind='needs_input')
    before = dict(board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    if entry == 'dashboard':
        from plugins.kanban.dashboard.plugin_api import _set_status_direct
        assert not _set_status_direct(board, tid, 'ready')
    elif entry == 'descendants':
        parent = kb.create_task(board, title='parent')
        with kb.write_txn(board):
            board.execute('INSERT INTO task_links(parent_id,child_id) VALUES (?,?)', (parent, tid))
        assert not kb.invalidate_descendants_for_parent_reopen(board, parent, author='test')['invalidated']
    elif entry == 'review':
        assert not kb.request_review(board, tid)
    elif entry == 'changes':
        assert not kb.request_changes(board, tid, reason='late')[0]
    elif entry == 'promote':
        assert not kb.promote_task(board, tid, actor='test')[0]
    elif entry == 'reopen':
        assert not kb.reopen_review_task(board, tid)
    else:
        assert not dispatch.adopt_worker_pid(board, tid, task.current_run_id, os.getpid())
    after = dict(board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    assert before == after
    assert termination.pending_requests(board, tid)[0]['id'] == request


@pytest.mark.parametrize('decision', ['stop', 'archive'])
def test_new_decision_supersedes_block_without_resurrection(board, decision):
    from hermes_cli.kanban_stop import stop_tasks, resume_tasks
    tid, task = claimed(board)
    request = termination.request_block(board, tid, reason='dependency', kind='needs_input')
    if decision == 'stop':
        stopped = stop_tasks(board, [tid], reason='new stop', author='test')[0]
    else:
        assert kb.archive_task(board, tid)
    saved = dict(board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    assert not termination.settle_request(board, tid, request)
    assert not termination.termination_pending(board, tid)
    assert dict(board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == saved
    if decision == 'stop':
        with kb.write_txn(board):
            kb._append_event(board, tid, 'notify_origin_changed', {'synthetic': True})
            kb._append_event(board, tid, 'heartbeat', {})
        assert resume_tasks(board, [tid], expected_stop_events={tid: stopped['event_id']}, reason='resume', author='test') == [tid]
    else:
        assert kb.get_task(board, tid).status == 'archived'


def test_marker_crash_recovers_without_heartbeat_or_comment_invalidating_settle(board):
    tid, task = claimed(board)
    request = termination.request_block(board, tid, reason='dependency', kind='needs_input')
    assert kb.get_task(board, tid).status == 'running'
    with kb.write_txn(board):
        kb._append_event(board, tid, 'heartbeat', {})
        kb._append_event(board, tid, 'commented', {'body': 'audit'})
    termination.reconcile_pending(board)
    assert kb.get_task(board, tid).status == 'blocked'
    assert not termination.termination_pending(board, tid)


def test_late_owned_spawn_remains_responsibility_until_exact_exit(board, worker):
    tid, task = claimed(board)
    with kb.write_txn(board):
        kb._append_event(board, tid, 'dispatch_intent', {'claim_lock': task.claim_lock}, run_id=task.current_run_id)
    request = termination.request_block(board, tid, reason='dependency', kind='needs_input')
    assert not termination.settle_request(board, tid, request)
    dispatch._set_worker_pid(board, tid, worker.pid, run_id=task.current_run_id, claim_lock=task.claim_lock)
    assert kb.get_task(board, tid).worker_pid is None
    assert not termination.settle_request(board, tid, request, terminate=False)
    assert worker.poll() is None
    assert termination.settle_request(board, tid, request)
    worker.wait(timeout=8)
    assert not termination.termination_pending(board, tid)


@pytest.mark.parametrize('kind', ['capability', 'transient'])
def test_nonhuman_block_recurrence_breaker_is_preserved(board, kind):
    tid, task = claimed(board)
    for index in range(kb.BLOCK_RECURRENCE_LIMIT):
        if index:
            assert kb.unblock_task(board, tid)
            task = kb.claim_task(board, tid)
        assert kb.block_task(board, tid, reason='repeat', kind=kind, expected_run_id=task.current_run_id)
    assert kb.get_task(board, tid).status == 'triage'


@pytest.mark.platforms('linux')
def test_marker_process_crash_before_signal_keeps_owned_worker_held(board, worker):
    tid, task = claimed(board)
    dispatch._set_worker_pid(board, tid, worker.pid, run_id=task.current_run_id, claim_lock=task.claim_lock)
    path = board.execute('PRAGMA database_list').fetchone()[2]
    code = '''
import os,sys
from pathlib import Path
from hermes_cli import kanban_db_connect as db, kanban_db_termination as term
assert Path(sys.argv[1]).resolve().is_relative_to(Path(os.environ['TMPDIR']).resolve())
with db.connect_closing(Path(sys.argv[1])) as conn:
    assert term.request_block(conn, sys.argv[2], reason='crash before signal', kind='needs_input')
os._exit(17)
'''
    with subprocess.Popen([sys.executable, '-B', '-c', code, path, tid]) as requester:
        assert requester.wait(timeout=20) == 17
    assert worker.poll() is None
    assert termination.termination_pending(board, tid)
    assert not kb.unblock_task(board, tid)
    assert not kb.reclaim_task(board, tid)
    assert kb.claim_task(board, tid) is None
    termination.reconcile_pending(board)
    worker.wait(timeout=8)
    assert kb.get_task(board, tid).status == 'blocked'
    assert not termination.termination_pending(board, tid)


def test_two_process_requests_share_one_task_responsibility(board, worker):
    tid, task = claimed(board)
    dispatch._set_worker_pid(board, tid, worker.pid, run_id=task.current_run_id, claim_lock=task.claim_lock)
    path = board.execute('PRAGMA database_list').fetchone()[2]
    code = '''
import json,os,sys
from pathlib import Path
from hermes_cli import kanban_db as kb, kanban_db_connect as db, kanban_db_termination as term
assert Path(sys.argv[1]).resolve().is_relative_to(Path(os.environ['TMPDIR']).resolve())
print('ready', flush=True)
sys.stdin.readline()
with db.connect_closing(Path(sys.argv[1])) as conn:
    request = term.request_block(conn, sys.argv[2], reason='concurrent block', kind='needs_input')
    print(json.dumps({'request': request, 'unblocked': kb.unblock_task(conn, sys.argv[2]),
                      'claimed': kb.claim_task(conn, sys.argv[2]) is not None}), flush=True)
'''
    processes = []
    try:
        for _ in range(2):
            processes.append(subprocess.Popen([sys.executable, '-B', '-c', code, path, tid],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for process in processes:
            assert process.stdout.readline().strip() == 'ready'
        for process in processes:
            process.stdin.write('go\n')
            process.stdin.flush()
        results = []
        for process in processes:
            output, error = process.communicate(timeout=20)
            assert process.returncode == 0, error
            results.append(json.loads(output))
        assert results[0] == results[1]
        assert results[0]['request'] == termination.pending_requests(board, tid)[0]['id']
        assert results[0]['unblocked'] is False and results[0]['claimed'] is False
        assert len(termination.pending_requests(board, tid)) == 1
        assert worker.poll() is None
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=8)


def test_actual_owner_spawn_after_marker_is_reserved_but_never_bootstrapped(board, monkeypatch):
    import asyncio
    from gateway import session_managed_worker as managed
    from gateway.session_kanban import owner_launch
    tid, task = claimed(board)
    path = board.execute('PRAGMA database_list').fetchone()[2]
    policy = SimpleNamespace(kanban_json=json.dumps({'db': path, 'task_id': tid,
        'run_id': task.current_run_id, 'claim_lock': task.claim_lock}))
    with kb.write_txn(board):
        kb._append_event(board, tid, 'owner_admitted', {}, run_id=task.current_run_id)
    assert owner_launch(policy, 'synthetic-admission')
    request = termination.request_block(board, tid, reason='between launch and spawn', kind='needs_input')
    assert not termination.settle_request(board, tid, request)
    assert not owner_launch(policy, 'fenced-admission')
    monkeypatch.setattr(managed, '_worker_env', lambda authority: dict(os.environ))
    authority = SimpleNamespace(profile_id=str(Path(path).parent))
    ref = SimpleNamespace(session_id='synthetic-owner')

    async def spawn():
        worker, process = await managed._spawn_registered_worker(authority, ref, {'generation': 1})
        try:
            hello = await worker.next_frame(20, ack=0)
            assert not owner_launch(policy, 'synthetic-admission', process=process, hello=hello)
            assert not termination.settle_request(board, tid, request, terminate=False)
            witness = board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='owner_worker_reserved'",
                                    (tid,)).fetchone()
            assert json.loads(witness[0])['pid'] == process.pid
            assert json.loads(witness[0])['binding'] == 'owned_popen_v1'
            assert process.poll() is None
            assert termination.settle_request(board, tid, request)
            process.wait(timeout=8)
        finally:
            worker.close()
    asyncio.run(spawn())
    assert kb.get_task(board, tid).status == 'blocked'
    assert not termination.termination_pending(board, tid)


@pytest.mark.parametrize('decision', ['stop', 'archive'])
def test_live_original_exit_settles_superseded_decision_without_resurrection(board, worker, monkeypatch, decision):
    from hermes_cli.kanban_stop import stop_tasks, resume_tasks
    tid, task = claimed(board)
    dispatch._set_worker_pid(board, tid, worker.pid, run_id=task.current_run_id, claim_lock=task.claim_lock)
    request = termination.request_block(board, tid, reason='dependency', kind='needs_input')
    monkeypatch.setattr(dispatch, '_terminate_reclaimed_worker', lambda *a, **kw: {
        'prev_pid': worker.pid, 'terminated': False, 'signal_refused': True})
    if decision == 'stop':
        stopped = stop_tasks(board, [tid], reason='new stop', author='test')[0]
        with pytest.raises(ValueError, match='BLOCKED_BY_TERMINATION'):
            resume_tasks(board, [tid], expected_stop_events={tid: stopped['event_id']}, reason='too early', author='test')
    else:
        def deny_signal(*args):
            raise PermissionError('synthetic archive signal refusal')
        assert kb.archive_task(board, tid, signal_fn=deny_signal)
    saved = dict(board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    assert worker.poll() is None
    assert not termination.settle_request(board, tid, request, terminate=False)
    assert termination.termination_pending(board, tid)
    assert kb.claim_task(board, tid) is None
    worker.terminate()
    worker.wait(timeout=8)
    assert not termination.settle_request(board, tid, request, terminate=False)
    assert not termination.termination_pending(board, tid)
    assert dict(board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == saved
    settlement = board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='block_termination_settled'",
                               (tid,)).fetchone()
    assert json.loads(settlement[0])['outcome'] == 'superseded'
    if decision == 'stop':
        assert resume_tasks(board, [tid], expected_stop_events={tid: stopped['event_id']}, reason='legal resume', author='test') == [tid]
        assert kb.claim_task(board, tid).current_run_id != task.current_run_id
    else:
        assert kb.get_task(board, tid).status == 'archived'


@pytest.mark.parametrize('decision', ['archive', 'resume'])
def test_old_owner_launch_cannot_reopen_a_closed_attempt(board, decision):
    from gateway.session_kanban import owner_launch
    from hermes_cli.kanban_stop import stop_tasks, resume_tasks
    tid, task = claimed(board)
    policy = SimpleNamespace(kanban_json=json.dumps({'db': board.execute('PRAGMA database_list').fetchone()[2],
        'task_id': tid, 'run_id': task.current_run_id, 'claim_lock': task.claim_lock}))
    if decision == 'archive':
        assert kb.archive_task(board, tid)
    else:
        stopped = stop_tasks(board, [tid], reason='stop', author='test')[0]
        assert resume_tasks(board, [tid], expected_stop_events={tid: stopped['event_id']}, reason='resume', author='test') == [tid]
        assert kb.claim_task(board, tid).current_run_id != task.current_run_id
    assert not owner_launch(policy, 'obsolete-admission')


def test_dispatch_publication_then_late_child_binding_refuses_execution(board, tmp_path, monkeypatch):
    tid = kb.create_task(board, title='actual synthetic dispatch', assignee='synthetic')
    kb.recompute_ready(board)
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda name: name == 'synthetic')
    monkeypatch.setattr(dispatch._kbw, 'resolve_workspace', lambda *a, **kw: tmp_path)
    monkeypatch.setattr(dispatch._kbw, '_maybe_emit_scratch_tip', lambda *a, **kw: None)
    monkeypatch.setattr(kb, '_fire_worker_spawned_hook', lambda *a, **kw: None)
    children = []
    code = '''
import json,sys
from gateway.session_kanban import bind_worker_context
from hermes_state_runtime import RuntimeStoreError
print('ready', flush=True)
context = json.loads(sys.stdin.readline())
try:
    bind_worker_context({'policy': {'source': 'kanban', 'kanban_json': json.dumps(context)}})
except RuntimeStoreError as exc:
    assert exc.reason == 'stale_kanban_claim'
    print('refused-before-execution', flush=True)
else:
    raise AssertionError('fenced worker reached execution')
'''
    def spawn(task, workspace):
        process = subprocess.Popen([sys.executable, '-B', '-c', code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(process)
        dispatch._live_worker_procs[process.pid] = process
        assert process.stdout.readline().strip() == 'ready'
        return process.pid
    try:
        assert dispatch._dispatch_lane_task(board, board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone(),
            'synthetic', dispatch.DispatchResult(), lane='ready', dry_run=False, ttl_seconds=300,
            board=None, failure_limit=kb.DEFAULT_FAILURE_LIMIT, spawn_fn=spawn,
            per_profile_cap=None, per_profile_running={})
        task = kb.get_task(board, tid)
        assert task.worker_pid == children[0].pid
        request = termination.request_block(board, tid, reason='before child bind', kind='needs_input')
        assert not termination.settle_request(board, tid, request, terminate=False)
        context = {'db': board.execute('PRAGMA database_list').fetchone()[2], 'task_id': tid,
                   'run_id': task.current_run_id, 'claim_lock': task.claim_lock,
                   'workspace': str(tmp_path), 'workspaces_root': str(tmp_path), 'profile': 'synthetic',
                   'branch': None, 'tenant': None, 'board': 'main', 'goal_mode': False,
                   'max_runtime_seconds': None}
        output, error = children[0].communicate(json.dumps(context) + '\n', timeout=20)
        assert children[0].returncode == 0, error
        assert output.strip() == 'refused-before-execution'
        assert termination.settle_request(board, tid, request, terminate=False)
        assert kb.get_task(board, tid).status == 'blocked'
        assert not termination.termination_pending(board, tid)
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=8)
            dispatch._live_worker_procs.pop(process.pid, None)


@pytest.mark.parametrize('refusal', ['identity', 'new_decision', 'budget'])
def test_superseded_settlement_does_not_bypass_resume_guards(board, refusal):
    from hermes_cli.kanban_stop import stop_tasks, resume_tasks
    tid, task = claimed(board)
    request = termination.request_block(board, tid, reason='dependency', kind='needs_input')
    stopped = stop_tasks(board, [tid], reason='stop', author='test')[0]
    assert not termination.settle_request(board, tid, request)
    assert not termination.termination_pending(board, tid)
    with kb.write_txn(board):
        if refusal == 'identity':
            row = board.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='block_termination_settled'", (tid,)).fetchone()
            data = json.loads(row['payload'])
            data['claim_lock'] = 'mismatched-original'
            board.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(data), row['id']))
        elif refusal == 'new_decision':
            kb._append_event(board, tid, 'status', {'status': 'blocked', 'reason': 'new human decision'})
        else:
            board.execute('UPDATE tasks SET consecutive_failures=? WHERE id=?', (kb.DEFAULT_FAILURE_LIMIT, tid))
    before = dict(board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    with pytest.raises(ValueError):
        resume_tasks(board, [tid], expected_stop_events={tid: stopped['event_id']}, reason='must refuse', author='test')
    assert dict(board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == before
