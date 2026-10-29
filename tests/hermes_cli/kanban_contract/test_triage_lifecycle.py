"""TODO174/175 coexistence, dispatch, native stops and late results; no actual workers."""
import inspect
import os
from pathlib import Path
import signal

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kbn

from test_triage_boundary import (
    db, create, escalated_review, resolve, snapshot, stub_model, gateway, MARK, OLD_BODY,
)


def dispatch(conn, monkeypatch):
    # Synthetic profile catalog and resource telemetry; keep real locks, ready
    # recomputation, retry guards, claims, run rows and workspace materialization.
    monkeypatch.setattr(kbd, '_profile_exists_fn', lambda: lambda name: name == 'synthetic-worker')
    monkeypatch.setattr(kbd, '_memory_pressure_level', lambda: 'normal')
    monkeypatch.setattr(kbd, 'review_dispatch_enabled', lambda: True)
    spawned = []
    def spawn(task, workspace, board=None):
        assert Path(kb.kanban_db_path()).resolve().is_relative_to(Path(os.environ['TODO198_ISOLATION']))
        assert Path(workspace).resolve().is_relative_to(Path(os.environ['TODO198_ISOLATION']))
        assert task.title.startswith(MARK)
        spawned.append((task.id, task.current_run_id, task.body))
        return None  # Explicit external-worker stub; no PID, process or model.
    result = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=100, failure_limit=5,
                               reconcile_orphans=False)
    assert len(result.spawned) == len(spawned)
    return spawned


@pytest.mark.parametrize('fanout', [False, True])
def test_gateway_to_dispatch_wait_stays_parked_but_unrelated_and_review_run(db, monkeypatch, fanout):
    wait = escalated_review(db)
    idea = create(db, triage=True)
    unrelated = create(db)
    review = create(db)
    assert kb.request_review(db, review, summary=MARK + ' ordinary review')
    calls = stub_model(monkeypatch, fanout=fanout)
    assert gateway(monkeypatch).auto_decompose_tick(10) == 1
    children = {task.id for task in kb.list_tasks(db)} - {wait, idea, unrelated, review}
    assert len(children) == (1 if fanout else 0)
    spawned = dispatch(db, monkeypatch)
    assert {row[0] for row in spawned} == {unrelated, review} | (children if fanout else {idea})
    assert calls == [idea]
    assert kb.get_task(db, wait).status == 'triage'
    assert kb.get_task(db, wait).body == OLD_BODY
    assert len(db.execute('SELECT * FROM task_runs WHERE ended_at IS NULL').fetchall()) == 3
    # Closing the synthetic runs uses the actual lifecycle (and leaves no orphan ownership).
    for tid, run_id, _ in spawned:
        assert kb.complete_task(db, tid, expected_run_id=run_id, result=MARK + ' synthetic result')
    assert not db.execute('SELECT 1 FROM task_runs WHERE ended_at IS NULL').fetchone()


@pytest.mark.parametrize('failures', [2, 5])
def test_explicit_cas_recovery_replaces_body_preserves_history_and_budget(db, monkeypatch, failures):
    tid = escalated_review(db)
    with kb.write_txn(db):
        db.execute('UPDATE tasks SET consecutive_failures=?, last_failure_error=? WHERE id=?',
                   (failures, MARK + ' earlier failure', tid))
    before = snapshot(db)
    previous = kb.get_task(db, tid)
    event_id = db.execute('SELECT MAX(id) FROM task_events WHERE task_id=?', (tid,)).fetchone()[0]
    assert resolve(db, tid, expected=event_id - 1) is None
    assert snapshot(db) == before
    result = resolve(db, tid, expected=event_id, body=MARK + ' NEW approved body; obligations A/B retained')
    assert result['status'] == 'review'
    assert result['budget_exhausted'] == (failures == 5)
    assert result['remaining_failures'] == 5 - failures
    after = kb.get_task(db, tid)
    for field in ['consecutive_failures', 'last_failure_error', 'block_kind', 'block_recurrences', 'max_retries', 'assignee']:
        assert getattr(after, field) == getattr(previous, field)
    assert snapshot(db)['task_runs'] == before['task_runs']
    assert snapshot(db)['task_events'][:-1] == before['task_events']
    assert 'NEW approved' in after.body
    assert resolve(db, tid, expected=event_id) is None
    spawned = dispatch(db, monkeypatch)
    # Existing TODO174 reports exhaustion but explicit recovery itself permits
    # dispatch. Verify the budget is not reset, and the next failure trips it.
    assert spawned[0][0] == tid and spawned[0][2] == after.body
    assert kb.get_task(db, tid).consecutive_failures == failures
    tripped = kbd._record_task_failure(db, tid, MARK + ' synthetic failure',
                                      outcome='spawn_failed', release_claim=True, end_run=True)
    assert tripped == (failures + 1 >= 5)
    assert kb.get_task(db, tid).consecutive_failures == failures + 1
    assert kb.get_task(db, tid).max_retries == 5


@pytest.mark.parametrize('field,value', [('claim_lock', MARK), ('claim_expires', 1), ('worker_pid', 123456789),
                                         ('current_run_id', 1), ('open_run', None)])
def test_explicit_recovery_refuses_occupied_triage(db, field, value):
    tid = escalated_review(db)
    with kb.write_txn(db):
        if field == 'open_run':
            db.execute("UPDATE task_runs SET ended_at=NULL, status='running' WHERE id="
                       '(SELECT MAX(id) FROM task_runs WHERE task_id=?)', (tid,))
        else:
            db.execute(f'UPDATE tasks SET {field}=? WHERE id=?', (value, tid))
    before = snapshot(db)
    assert resolve(db, tid) is None
    assert snapshot(db) == before


def test_dependency_and_review_parent_regating_recover_normally(db, monkeypatch):
    parent = create(db)
    child = create(db)
    claim = kb.claim_task(db, child, claimer=MARK)
    assert claim
    link_kwargs = ({'expected_child_run_id': claim.current_run_id}
                   if 'expected_child_run_id' in inspect.signature(kb.link_tasks).parameters else {})
    kb.link_tasks(db, parent, child, **link_kwargs)
    assert kb.block_task(db, child, kind='dependency', reason=MARK + ' upstream pending')
    assert kb.get_task(db, child).status == 'todo'
    kb.recompute_ready(db)
    assert kb.get_task(db, child).status == 'todo'
    reviewed = escalated_review(db)
    kb.link_tasks(db, parent, reviewed)
    assert resolve(db, reviewed)['status'] == 'todo'
    assert kb.claim_review_task(db, reviewed, claimer=MARK) is None
    assert kb.complete_task(db, parent, result=MARK + ' parent complete')
    kb.recompute_ready(db)
    assert kb.get_task(db, child).status == 'ready'
    assert kb.get_task(db, reviewed).status == 'review'
    assert {row[0] for row in dispatch(db, monkeypatch)} == {child, reviewed}


def test_transient_normal_retry_then_escalation_is_not_frozen_or_reset(db, monkeypatch):
    tid = create(db)
    assert kb.claim_task(db, tid, claimer=MARK)
    assert kb.block_task(db, tid, kind='transient', reason=MARK + ' temporary service wait')
    assert kb.get_task(db, tid).status == 'blocked'
    kb.recompute_ready(db)
    assert kb.get_task(db, tid).status == 'blocked'  # Explicit block is sticky, even transient.
    assert kb.unblock_task(db, tid)
    assert kb.get_task(db, tid).status == 'ready'
    assert kb.get_task(db, tid).block_recurrences == 1
    assert kb.claim_task(db, tid, claimer=MARK)
    assert kb.block_task(db, tid, kind='transient', reason=MARK + ' same temporary wait')
    assert kb.get_task(db, tid).status == 'triage'
    stub_model(monkeypatch)
    assert gateway(monkeypatch).auto_decompose_tick(1) == 0
    assert dispatch(db, monkeypatch) == []
    assert kb.get_task(db, tid).block_recurrences == kb.BLOCK_RECURRENCE_LIMIT


def test_native_blocked_pause_survives_ticks_comments_are_not_a_stop(db, monkeypatch):
    paused = create(db, initial_status='blocked')
    commented = create(db)
    kb.add_comment(db, commented, author=MARK, body='Stop all work. 等我确认，不要继续。')
    stub_model(monkeypatch)
    assert gateway(monkeypatch).auto_decompose_tick(1) == 0
    assert [row[0] for row in dispatch(db, monkeypatch)] == [commented]
    assert kb.get_task(db, paused).status == 'blocked'
    assert kb.unblock_task(db, paused)
    assert kb.claim_task(db, paused, claimer=MARK)


@pytest.mark.parametrize('state', ['triage', 'archived', 'new_run'])
def test_late_worker_result_and_notification_cannot_revive_or_clobber(db, monkeypatch, state):
    tid = escalated_review(db)
    stale_run = db.execute('SELECT MAX(id) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0]
    if state == 'archived':
        assert kb.archive_task(db, tid)
    elif state == 'new_run':
        assert resolve(db, tid)
        assert kb.claim_review_task(db, tid, claimer=MARK)
    route = dict(task_id=tid, platform='synthetic', chat_id=MARK)
    kbn.add_notify_sub(db, **route)
    # Consume only existing history using the real cursor-CAS, never a transport.
    kbn.claim_unseen_events_for_sub(db, **route)
    before = snapshot(db)
    assert not kb.complete_task(db, tid, expected_run_id=stale_run, result=MARK + ' LATE result')
    assert not kb.block_task(db, tid, expected_run_id=stale_run, kind='transient', reason=MARK + ' LATE block')
    assert not kb.request_review(db, tid, expected_run_id=stale_run, summary=MARK + ' LATE handoff')
    assert not kbd.heartbeat_worker(db, tid, expected_run_id=stale_run)
    assert snapshot(db) == before
    assert kbn.claim_unseen_events_for_sub(db, **route)[2] == []
    # Notification cursor processing is not an authorization or lifecycle mutation.
    assert snapshot(db) == before


def test_archive_commits_before_local_worker_termination_and_late_result(db, monkeypatch):
    tid = create(db)
    claim = kb.claim_task(db, tid)  # Native host-local lock, not a foreign claimer.
    assert claim
    pid = 123456789
    if hasattr(kbd, '_process_fingerprint'):
        # Fork checks process fingerprints as well as host ownership. No real PID.
        monkeypatch.setattr(kbd, '_process_fingerprint', lambda target: MARK + '|123')
    kbd._set_worker_pid(db, tid, pid)
    signals = []
    def signal_stub(target, sig):
        assert target == pid and sig == signal.SIGTERM
        with kbc.connect_closing() as other:
            task = kb.get_task(other, tid)
            assert task.status == 'archived'
            assert task.current_run_id is None and task.claim_lock is None and task.worker_pid is None
            assert other.execute('SELECT ended_at FROM task_runs WHERE id=?', (claim.current_run_id,)).fetchone()[0]
        signals.append((target, sig))
        raise ProcessLookupError  # Synthetic external kill seam; no real signal.
    assert kb.archive_task(db, tid, signal_fn=signal_stub)
    assert signals == [(pid, signal.SIGTERM)]
    assert not kb.archive_task(db, tid, signal_fn=signal_stub)
    assert len(signals) == 1
    assert not kb.complete_task(db, tid, expected_run_id=claim.current_run_id, result=MARK + ' LATE')
    termination = [e for e in kb.list_events(db, tid) if e.kind == 'archive_worker_termination'][-1]
    assert termination.payload['host_local'] and termination.payload['terminated']
    assert dispatch(db, monkeypatch) == []


def test_normal_failure_retries_keep_budget_then_breaker_stops(db, monkeypatch):
    tid = create(db)
    for failures in range(1, 6):
        assert kb.claim_task(db, tid, claimer=MARK)
        tripped = kbd._record_task_failure(db, tid, MARK + ' temporary spawn failure',
                                          outcome='spawn_failed', release_claim=True, end_run=True)
        assert tripped == (failures == 5)
        assert kb.get_task(db, tid).consecutive_failures == failures
        assert kb.get_task(db, tid).status == ('blocked' if tripped else 'ready')
    kb.recompute_ready(db)
    assert kb.get_task(db, tid).status == 'blocked'
    assert dispatch(db, monkeypatch) == []
    assert kb.get_task(db, tid).max_retries == 5
    assert not db.execute('SELECT 1 FROM task_runs WHERE ended_at IS NULL').fetchone()


def test_queued_old_notification_cursor_cannot_authorize_archived_work(db, monkeypatch):
    tid = create(db)
    route = dict(task_id=tid, platform='synthetic', chat_id=MARK)
    kbn.add_notify_sub(db, **route)
    assert kb.claim_task(db, tid, claimer=MARK)
    assert kb.request_review(db, tid, force=True, summary=MARK + ' old handoff')
    assert kb.archive_task(db, tid)
    before = snapshot(db)
    with kbc.connect_closing() as other:
        _, _, events = kbn.claim_unseen_events_for_sub(other, **route)
    assert {'review_requested', 'archived'} <= {event.kind for event in events}
    assert kbn.claim_unseen_events_for_sub(db, **route)[2] == []
    assert snapshot(db) == before
    stub_model(monkeypatch)
    assert gateway(monkeypatch).auto_decompose_tick(1) == 0
    assert dispatch(db, monkeypatch) == []
