"""Supported orchestrator tools, actual private-PID sleep worker and real dispatcher DB."""
import json
import os
from pathlib import Path
import subprocess
import sys

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from tools import kanban_tools as tools


def test_stop_tool_fences_worker_successors_and_resume(monkeypatch):
    assert hasattr(tools, '_handle_stop'), 'No supported task-set stop entry: /loop stop is supervision only'
    monkeypatch.setattr(tools, '_profile_has_kanban_toolset', lambda: True)
    path = Path(os.environ['HERMES_KANBAN_DB']).resolve()
    assert path.is_relative_to(Path(os.environ['TODO199_RUN']))
    with kbc.connect(db_path=path) as conn:
        root = kb.create_task(conn, title='running', assignee='synthetic', max_retries=5)
        child = kb.create_task(conn, title='successor', assignee='synthetic', parents=[root])
        other = kb.create_task(conn, title='unrelated', assignee='synthetic')
        claimed = kb.claim_task(conn, root)
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        try:
            dispatch._set_worker_pid(conn, root, proc.pid, run_id=claimed.current_run_id, claim_lock=claimed.claim_lock)
            with kb.write_txn(conn):
                conn.execute('UPDATE tasks SET consecutive_failures=2 WHERE id=?', (root,))
                conn.execute('UPDATE task_runs SET summary=?, error=?, metadata=? WHERE id=?',
                             ('saved partial result', 'prior fault', json.dumps({'pending_obligation': 'deliver result'}),
                              claimed.current_run_id))
            result = json.loads(tools._handle_stop({'task_ids': [root, child], 'reason': 'user stopped this set'}))
            assert result.get('ok'), result
            proc.wait(timeout=8)
            assert proc.returncode != 0
            saved = conn.execute('SELECT summary, error, metadata FROM task_runs WHERE id=?',
                                 (claimed.current_run_id,)).fetchone()
            assert saved['summary'] == 'saved partial result' and saved['error'] == 'prior fault'
            assert json.loads(saved['metadata']) == {'pending_obligation': 'deliver result'}
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, root) is None and kb.claim_task(conn, child) is None
            assert kb.claim_task(conn, other) is not None
            assert kb.get_task(conn, root).consecutive_failures == 2
            assert not kb.unblock_task(conn, root), 'generic unblock must not erase explicit stop'
            assert not kb.specify_triage_task(conn, root, body='late old result')
            stopped = result['stopped']
            resume = json.loads(tools._handle_resume({'task_ids': [root, child],
                'expected_stop_events': {item['task_id']: item['event_id'] for item in stopped},
                'reason': 'explicit resume within original authorization'}))
            assert resume.get('ok'), resume
            assert kb.get_task(conn, root).consecutive_failures == 2
            assert kb.claim_task(conn, root) is not None
            assert kb.claim_task(conn, child) is None
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)


def _stop(ids):
    result = json.loads(tools._handle_stop({'task_ids': ids, 'reason': 'confirmed exact synthetic set'}))
    assert result.get('ok'), result
    return result


def _resume(ids, result):
    return json.loads(tools._handle_resume({'task_ids': ids, 'reason': 'explicit original authorization',
        'expected_stop_events': {item['task_id']: item['event_id'] for item in result['stopped']}}))


def test_actual_dispatcher_rechecks_stop_between_claim_and_spawn(monkeypatch):
    from hermes_cli import kanban_db_workspace as workspace
    monkeypatch.setattr(tools, '_profile_has_kanban_toolset', lambda: True)
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda name: True)
    monkeypatch.setattr(dispatch, '_memory_pressure_level', lambda: 'normal')
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='race', assignee='synthetic', workspace_kind='scratch')
        real = workspace.resolve_workspace
        def paused(task, **kw):
            result = real(task, **kw)
            _stop([tid])  # deterministic real handler interleave AFTER claim, BEFORE spawn
            return result
        monkeypatch.setattr(workspace, 'resolve_workspace', paused)
        calls = []
        outcome = dispatch.dispatch_once(conn, spawn_fn=lambda *a, **kw: calls.append(a), reconcile_orphans=False)
        assert not calls and not outcome.spawned
        assert kb.get_task(conn, tid).status == 'blocked'


def test_root_only_stop_requires_confirmed_active_descendant_set(monkeypatch):
    monkeypatch.setattr(tools, '_profile_has_kanban_toolset', lambda: True)
    with kbc.connect() as conn:
        root = kb.create_task(conn, title='finished upstream', assignee='synthetic')
        child = kb.create_task(conn, title='already claimed descendant', assignee='synthetic', parents=[root])
        assert kb.complete_task(conn, root, result='prior real result')
        assert kb.claim_task(conn, child)
        refused = json.loads(tools._handle_stop({'task_ids': [root], 'reason': 'root only'}))
        assert not refused.get('ok') and child in refused['error']
        assert kb.get_task(conn, child).status == 'running'
        assert not any(e.kind == 'execution_stopped' for e in kb.list_events(conn, root))
        _stop([root, child])
        assert kb.get_task(conn, child).status == 'blocked'


def test_inflight_specify_fanout_and_fresh_process_cannot_revive(monkeypatch):
    from test_triage_boundary import stub_model
    from hermes_cli import kanban_decompose as kd
    from hermes_cli import kanban_specify as ks
    monkeypatch.setattr(tools, '_profile_has_kanban_toolset', lambda: True)
    with kbc.connect() as conn:
        for fanout in (False, True):
            tid = kb.create_task(conn, title='rough idea', assignee='synthetic-worker', triage=True)
            calls = stub_model(monkeypatch, fanout=fanout, callback=lambda: _stop([tid]))
            assert not kd.decompose_task(tid, author='synthetic').ok
            assert len(calls) == 1 and kb.get_task(conn, tid).status == 'blocked'
            assert not conn.execute('SELECT 1 FROM task_links WHERE parent_id=?', (tid,)).fetchone()
        script = """
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_decompose as kd, kanban_db_dispatch as d
with kbc.connect() as conn:
    assert kd.list_triage_ids() == []
    kb.recompute_ready(conn)
    assert all(kb.claim_task(conn, t.id) is None for t in kb.list_tasks(conn))
    assert not d.dispatch_once(conn, spawn_fn=lambda *a, **kw: (_ for _ in ()).throw(AssertionError('revived')), reconcile_orphans=False).spawned
print('RESTART_STOP_FENCE_OK')
"""
        out = subprocess.check_output([sys.executable, '-c', script], text=True)
        assert 'RESTART_STOP_FENCE_OK' in out
        print(out)


def test_supported_specify_rejects_late_result_and_resume_requires_fresh_decision(monkeypatch):
    from hermes_cli import kanban_specify as ks
    monkeypatch.setattr(tools, '_profile_has_kanban_toolset', lambda: True)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='rough', body='original authorized obligations', assignee='synthetic', triage=True)
        stopped = []
        def auxiliary(*args, **kw):
            stopped.append(_stop([tid]))
            return json.dumps({'title': 'STALE', 'body': 'STALE MODEL RESULT'}), ''
        monkeypatch.setattr(ks, '_call_aux', auxiliary)
        assert not ks.specify_task(tid, author='synthetic').ok
        assert kb.get_task(conn, tid).body == 'original authorized obligations'
        assert _resume([tid], stopped[0]).get('ok')
        assert kb.get_task(conn, tid).status == 'triage'
        assert not kb.specify_triage_task(conn, tid, body='OLD MODEL RESULT')
        latest = conn.execute('SELECT MAX(id) FROM task_events WHERE task_id=?', (tid,)).fetchone()[0]
        resolved = json.loads(tools._handle_unblock({'task_id': tid, 'resolve_triage': True,
            'expected_event_id': latest, 'reason': 'original authorization still sufficient',
            'body': 'fresh reviewed obligations'}))
        assert resolved.get('ok'), resolved
        assert kb.claim_task(conn, tid)


def test_stopped_ancestor_fences_inflight_descendant_fanout(monkeypatch):
    from hermes_cli import kanban_decompose as kd
    from test_triage_boundary import stub_model
    monkeypatch.setattr(tools, '_profile_has_kanban_toolset', lambda: True)
    with kbc.connect() as conn:
        root = kb.create_task(conn, title='ancestor', assignee='synthetic-worker')
        idea = kb.create_task(conn, title='child idea', assignee='synthetic-worker', triage=True, parents=[root])
        calls = stub_model(monkeypatch, fanout=True, callback=lambda: _stop([root]))
        assert not kd.decompose_task(idea, author='synthetic').ok
        assert len(calls) == 1
        assert not conn.execute('SELECT 1 FROM task_links WHERE parent_id=?', (idea,)).fetchone()


def test_late_result_visible_resume_preserves_review_budget_and_new_status_wins(monkeypatch):
    from hermes_cli import kanban_db_notify as notify
    from test_notifications import pool
    monkeypatch.setattr(tools, '_profile_has_kanban_toolset', lambda: True)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='review worker', assignee='synthetic', max_retries=5)
        assert kb.request_review(conn, tid, summary='review obligations')
        run = kb.claim_review_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET consecutive_failures=2, block_recurrences=1 WHERE id=?', (tid,))
        stopped = _stop([tid])
        notify.add_notify_sub(conn, task_id=tid, platform='tui', chat_id='session-one')
        assert not kb.complete_task(conn, tid, expected_run_id=run.current_run_id, result='LATE REAL NEW RESULT')
        monkeypatch.setattr(kb, 'list_boards', lambda **kw: [{'slug': 'default', 'db_path': os.environ['HERMES_KANBAN_DB']}])
        events = pool()._claim_kanban_notifications({'session-one'})
        assert len(events) == 1 and events[0]['event']['kind'] == 'late_result'
        assert 'LATE REAL NEW RESULT' in events[0]['message'] and events[0]['execution_stopped']
        assert kb.get_task(conn, tid).status == 'blocked'
        assert _resume([tid], stopped).get('ok')
        after = kb.get_task(conn, tid)
        assert after.status == 'review' and after.consecutive_failures == 2 and after.block_recurrences == 1
        assert kb.claim_review_task(conn, tid)
        stopped = _stop([tid])
        assert kb.archive_task(conn, tid)
        assert not _resume([tid], stopped).get('ok')
        assert kb.get_task(conn, tid).status == 'archived'


def test_dispatcher_spawn_and_stop_are_ordered_with_actual_process(monkeypatch):
    import threading
    monkeypatch.setattr(tools, '_profile_has_kanban_toolset', lambda: True)
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda name: True)
    monkeypatch.setattr(dispatch, '_memory_pressure_level', lambda: 'normal')
    started = threading.Event()
    stopping = threading.Event()
    reports = []
    workers = []
    with kbc.connect() as conn:
        root = kb.create_task(conn, title='done ancestor', assignee='synthetic', workspace_kind='scratch')
        child = kb.create_task(conn, title='spawning child', assignee='synthetic', parents=[root], workspace_kind='scratch')
        assert kb.complete_task(conn, root, result='predecessor done')
        def stop_thread():
            assert started.wait(5)
            stopping.set()
            reports.append(_stop([root, child]))
        thread = threading.Thread(target=stop_thread)
        thread.start()
        def spawn(task, workspace, **kw):
            assert task.id == child
            proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
            workers.append(proc)
            started.set()
            assert stopping.wait(5)
            return proc.pid
        try:
            dispatched = dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, reconcile_orphans=False)
            thread.join(10)
            assert not thread.is_alive() and reports
            assert [entry[0] for entry in dispatched.spawned] == [child]
            workers[0].wait(timeout=5)
            assert workers[0].returncode != 0
            assert kb.claim_task(conn, child) is None
            assert not dispatch.dispatch_once(conn, spawn_fn=lambda *a, **kw: (_ for _ in ()).throw(AssertionError('revived')),
                                              reconcile_orphans=False).spawned
        finally:
            for proc in workers:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)


def test_restart_reaper_finishes_stop_interrupted_after_commit(monkeypatch):
    monkeypatch.setattr(tools, '_profile_has_kanban_toolset', lambda: True)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='stop interruption', assignee='synthetic')
        claim = kb.claim_task(conn, tid)
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        dispatch._set_worker_pid(conn, tid, proc.pid, run_id=claim.current_run_id, claim_lock=claim.claim_lock)
        bound_proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'worker_bound', {'pid': bound_proc.pid, 'claim_lock': claim.claim_lock,
                'started_at': dispatch._process_fingerprint(bound_proc.pid)}, run_id=claim.current_run_id)
        try:
            script = """
import os, sys
from tools import kanban_tools as t
from hermes_cli import kanban_db_dispatch as d
t._profile_has_kanban_toolset=lambda: True
d._terminate_reclaimed_worker=lambda *a, **kw: os._exit(17)
t._handle_stop({'task_ids':[sys.argv[1]],'reason':'synthetic crash after commit'})
"""
            result = subprocess.run([sys.executable, '-c', script, tid])
            assert result.returncode == 17 and proc.poll() is None
            assert kb.get_task(conn, tid).status == 'blocked'
            script = """
from hermes_cli import kanban_db_connect as c, kanban_db_dispatch as d, kanban_db as kb
d.TERMINAL_WORKER_REAP_GRACE_SECONDS=0  # explicit clock seam; actual identity-checked signal
with c.connect() as conn:
    assert d.reap_terminal_workers(conn)
    assert all(kb.claim_task(conn,t.id) is None for t in kb.list_tasks(conn))
print('RESTART_TERMINATION_OK')
"""
            assert 'RESTART_TERMINATION_OK' in subprocess.check_output([sys.executable, '-c', script], text=True)
            proc.wait(timeout=5)
            bound_proc.wait(timeout=5)
            assert proc.returncode != 0 and bound_proc.returncode != 0
        finally:
            for worker in (proc, bound_proc):
                if worker.poll() is None:
                    worker.terminate()
                    worker.wait(timeout=5)


def test_managed_worker_binding_cannot_revive_and_real_result_is_retained(monkeypatch):
    import pytest
    from gateway import session_kanban as managed
    from hermes_state_runtime import RuntimeStoreError
    monkeypatch.setattr(tools, '_profile_has_kanban_toolset', lambda: True)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='bound interpreter', assignee='synthetic')
        claim = kb.claim_task(conn, tid)
        # This test process represents an already-bound interpreter. The stop
        # entry must refuse self-signalling while still committing the fence.
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'worker_bound', {'pid': os.getpid(), 'claim_lock': claim.claim_lock,
                'started_at': dispatch._process_fingerprint(os.getpid())}, run_id=claim.current_run_id)
        stopped = _stop([tid])
        assert not stopped['stopped'][0]['termination']['terminated']
        context = {'task_id': tid, 'run_id': claim.current_run_id, 'claim_lock': claim.claim_lock,
            'db': os.environ['HERMES_KANBAN_DB'], 'board': 'default', 'workspace': os.environ['TODO199_RUN'],
            'workspaces_root': os.environ['TODO199_RUN'], 'profile': 'synthetic', 'branch': None,
            'tenant': None, 'goal_mode': False, 'max_runtime_seconds': 60}
        managed._record_worker_result(context, 0, 'REAL LATE PROTOCOL OUTPUT')
        managed._TURNS_DONE.clear()
        late = [event for event in kb.list_events(conn, tid) if event.kind == 'late_result']
        assert late[-1].payload['summary'] == 'REAL LATE PROTOCOL OUTPUT'
        assert kb.get_task(conn, tid).status == 'blocked'
        with pytest.raises(RuntimeStoreError, match='stale_kanban_claim'):
            managed.bind_worker_context({'policy': {'source': 'kanban', 'kanban_json': json.dumps(context)}})
        assert not _resume([tid], stopped).get('ok')


def test_loop_stop_is_only_supervision():
    import sqlite3
    sys.path.insert(0, '/web/packages/server/src/modules/hermes/services/bridge/python')
    from bridge_loops import LoopStore
    home = Path(os.environ['HERMES_HOME'])
    assert home.resolve().is_relative_to(Path(os.environ['TODO199_RUN']))
    with sqlite3.connect(home / 'state.db') as state:
        state.execute('CREATE TABLE IF NOT EXISTS state_meta (key TEXT PRIMARY KEY, value TEXT)')
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='loop is not work stop', assignee='synthetic')
        claim = kb.claim_task(conn, tid)
        result = LoopStore(home).command('synthetic-session', 'stop', 'default')
        assert result['ok'], result
        assert kb.get_task(conn, tid).current_run_id == claim.current_run_id
        assert kb.get_task(conn, tid).status == 'running'
        assert not any(e.kind == 'execution_stopped' for e in kb.list_events(conn, tid))


def test_registered_tools_and_exhausted_budget(monkeypatch):
    import toolsets
    from tools.registry import registry
    monkeypatch.setattr(tools, '_profile_has_kanban_toolset', lambda: True)
    assert 'kanban_stop' in toolsets._HERMES_CORE_TOOLS
    assert registry.get_schema('kanban_stop')['parameters']['required'] == ['task_ids', 'reason']
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='budget', assignee='synthetic', max_retries=2)
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET consecutive_failures=2 WHERE id=?', (tid,))
        stopped = _stop([tid])
        assert not _resume([tid], stopped).get('ok')
        assert kb.get_task(conn, tid).consecutive_failures == 2
