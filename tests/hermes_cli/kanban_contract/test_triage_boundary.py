"""TODO198 synthetic contract tests. Auxiliary replies are STUBS, never actual LLM output."""
import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as kd
from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _DispatcherSettings

MARK = 'TODO198-SYNTHETIC'
OLD_BODY = 'Wait for explicit human input; unfinished obligations: A and B.'

@pytest.fixture
def db(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    path = (tmp_path / 'synthetic.db').resolve()
    assert path.is_relative_to(root)
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    assert Path(kb.kanban_db_path()).resolve() == path
    with kbc.connect_closing() as conn:
        assert Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve() == path
        yield conn


def create(conn, **kwargs):
    return kb.create_task(conn, title=MARK + ' rough task', body=OLD_BODY,
                          assignee='synthetic-worker', workspace_kind='scratch',
                          max_retries=5, **kwargs)


def escalated_review(conn, kind='needs_input'):
    tid = create(conn)
    assert kb.claim_task(conn, tid, claimer=MARK)
    assert kb.request_review(conn, tid, summary=MARK + ' review obligations A/B', force=True)
    assert kb.claim_review_task(conn, tid, claimer=MARK)
    assert kb.block_task(conn, tid, reason=MARK + ' human wait', kind=kind)
    assert kb.get_task(conn, tid).status == 'blocked'
    assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == 'review'
    assert kb.claim_review_task(conn, tid, claimer=MARK)
    assert kb.block_task(conn, tid, reason=MARK + ' human wait', kind=kind)
    assert kb.get_task(conn, tid).status == 'triage'
    return tid


def stub_model(monkeypatch, *, fanout=False, callback=None):
    calls = []
    routing = kd._Routing('synthetic-worker', 'synthetic-worker', True,
                          [{'name': 'synthetic-worker', 'description': MARK, 'has_description': True}],
                          {'synthetic-worker'})
    monkeypatch.setattr(kd, '_load_routing', lambda **kwargs: routing)
    def aux(*args, **kwargs):
        calls.append(args[1])
        if callback:
            callback()
        reply = {'fanout': fanout, 'title': MARK + ' STUB rewritten', 'body': MARK + ' STUB actionable',
                 'tasks': [{'title': MARK + ' STUB child', 'body': MARK, 'assignee': 'synthetic-worker'}]}
        return json.dumps(reply), ''
    monkeypatch.setattr(kd, '_call_aux', aux)
    return calls


def gateway(monkeypatch):
    settings = _DispatcherSettings(60, 1, 1, 5, 0, True, 'synthetic-worker', 1)
    dispatcher = _KanbanDispatcher(kb, settings)
    monkeypatch.setattr(dispatcher, '_board_slugs', lambda: ['default'])
    return dispatcher


def test_exact_repeated_human_wait_not_auto_rewritten_or_claimed(db, monkeypatch):
    tid = escalated_review(db)
    before = kb.get_task(db, tid)
    events_before = len(kb.list_events(db, tid))
    calls = stub_model(monkeypatch)
    successes = gateway(monkeypatch).auto_decompose_tick(5)
    kb.recompute_ready(db)
    # Keep the real claim CAS: this is the dangerous point immediately before external spawn.
    claim = kb.claim_review_task(db, tid, claimer=MARK + '-would-spawn')
    after = kb.get_task(db, tid)
    evidence = {'aux_stub_calls': calls, 'auto_decompose_successes': successes,
                'before_status': before.status, 'after_status': after.status,
                'body_rewritten': after.body != before.body, 'claim_succeeded': claim is not None,
                'events_added': len(kb.list_events(db, tid)) - events_before}
    print('TODO198_EXACT_SYMPTOM ' + json.dumps(evidence))
    assert after.status == 'triage' and after.body == before.body and claim is None, evidence
    assert calls == [] and successes == 0


def test_exact_fanout_does_not_create_actionable_children_from_wait(db, monkeypatch):
    tid = escalated_review(db)
    calls = stub_model(monkeypatch, fanout=True)
    successes = gateway(monkeypatch).auto_decompose_tick(1)
    children = [task for task in kb.list_tasks(db) if task.id != tid]
    claimed = [kb.claim_task(db, child.id, claimer=MARK) for child in children]
    evidence = {'aux_stub_calls': calls, 'auto_decompose_successes': successes,
                'root_status': kb.get_task(db, tid).status, 'child_count': len(children),
                'child_claims': sum(task is not None for task in claimed)}
    print('TODO198_FANOUT_SYMPTOM ' + json.dumps(evidence))
    assert children == [] and kb.get_task(db, tid).status == 'triage', evidence
    assert successes == 0 and calls == []


def snapshot(conn):
    return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
            for table in ['tasks', 'task_runs', 'task_events', 'task_links', 'task_comments']}


@pytest.mark.parametrize('kind', ['needs_input', 'capability', None, 'transient'])
@pytest.mark.parametrize('fanout', [False, True])
def test_escalations_rejected_in_lists_load_and_both_writers(db, monkeypatch, kind, fanout):
    from hermes_cli import kanban_specify as ks
    from hermes_cli.kanban_db_graph import decompose_triage_task
    tid = escalated_review(db, kind)
    before = snapshot(db)
    calls = stub_model(monkeypatch, fanout=fanout)
    monkeypatch.setattr(ks, '_call_aux', lambda *a, **kw: pytest.fail('specifier called model for wait'))
    assert tid not in kd.list_triage_ids()
    assert tid not in ks.list_triage_ids()
    assert not ks.specify_task(tid, author=MARK).ok
    assert not kd.decompose_task(tid, author=MARK).ok
    assert not kb.specify_triage_task(db, tid, title=MARK + ' forbidden', body='forbidden')
    assert decompose_triage_task(db, tid, root_assignee='synthetic-worker',
                                children=[{'title': MARK + ' forbidden child'}]) is None
    assert calls == []
    assert snapshot(db) == before


@pytest.mark.parametrize('fanout', [False, True])
def test_initial_idea_remains_eligible_and_does_not_starve_behind_wait(db, monkeypatch, fanout):
    from hermes_cli import kanban_specify as ks
    wait = escalated_review(db)
    idea = create(db, triage=True, tenant=MARK)
    kb.add_comment(db, idea, author=MARK, body='Stop. 暂停。This comment is NOT lifecycle authorization.')
    assert ks.list_triage_ids(tenant=MARK) == [idea]
    calls = stub_model(monkeypatch, fanout=fanout)
    assert gateway(monkeypatch).auto_decompose_tick(1) == 1
    assert calls == [idea]
    assert kb.get_task(db, wait).status == 'triage'
    assert kb.get_task(db, wait).body == OLD_BODY
    assert kb.get_task(db, idea).status == ('todo' if fanout else 'ready')
    assert gateway(monkeypatch).auto_decompose_tick(1) == 0


@pytest.mark.parametrize('provenance', ['blocked', 'block_loop_detected', 'status', 'unknown', 'missing', 'created_ready'])
def test_legacy_null_block_kind_requires_safe_event_provenance(db, monkeypatch, provenance):
    tid = create(db, triage=True)
    with kb.write_txn(db):
        if provenance == 'missing':
            db.execute('DELETE FROM task_events WHERE task_id=?', (tid,))
        elif provenance == 'created_ready':
            db.execute('UPDATE task_events SET payload=? WHERE task_id=?',
                       (json.dumps({'status': 'ready'}), tid))
        else:
            kb._append_event(db, tid, provenance, {'status': 'triage', 'previous_status': 'blocked'})
    kb.add_comment(db, tid, author=MARK, body='Latest annotation must not hide older provenance.')
    assert kb.get_task(db, tid).block_kind is None
    before = snapshot(db)
    calls = stub_model(monkeypatch)
    assert gateway(monkeypatch).auto_decompose_tick(1) == 0
    assert calls == [] and snapshot(db) == before


@pytest.mark.parametrize('field,value', [('claim_lock', MARK), ('claim_expires', 1), ('worker_pid', 123456789),
                                         ('current_run_id', 1), ('consecutive_failures', 2), ('block_recurrences', 2)])
def test_inconsistent_occupied_or_budgeted_triage_fails_closed(db, field, value):
    tid = create(db, triage=True)
    with kb.write_txn(db):
        db.execute(f'UPDATE tasks SET {field}=? WHERE id=?', (value, tid))
    before = snapshot(db)
    assert not kb.specify_triage_task(db, tid, body=MARK + ' no overwrite')
    assert snapshot(db) == before


def resolve(conn, tid, body=MARK + ' explicitly authorized replacement', expected=None):
    if hasattr(kb, 'resolve_triage_task'):
        fn = kb.resolve_triage_task
    else:
        from hermes_cli.kanban_triage import resolve_triage_task
        fn = resolve_triage_task
    event_id = conn.execute('SELECT MAX(id) FROM task_events WHERE task_id=?', (tid,)).fetchone()[0]
    return fn(conn, tid, reason=MARK + ' explicit operator decision',
              expected_event_id=event_id if expected is None else expected, body=body, author=MARK)


@pytest.mark.parametrize('mode', ['specify', 'single', 'fanout'])
@pytest.mark.parametrize('race', ['archive', 'archive_retriaged', 'blocked', 'aba_block', 'recovered', 'recovered_retriaged'])
def test_inflight_result_cannot_overwrite_stop_or_recovery(db, monkeypatch, mode, race):
    from hermes_cli import kanban_specify as ks
    tid = create(db, triage=True)
    after_race = []
    def change_while_model_is_away():
        # Separate connection commits before the old auxiliary response arrives.
        with kbc.connect_closing() as other:
            if race in {'archive', 'archive_retriaged'}:
                assert kb.archive_task(other, tid)
                if race == 'archive_retriaged':
                    # Synthetic legacy dashboard status mutation, with durable provenance.
                    with kb.write_txn(other):
                        other.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
                        kb._append_event(other, tid, 'status', {'previous_status': 'archived', 'status': 'triage'})
            else:
                assert resolve(other, tid)
                if race != 'recovered':
                    assert kb.claim_task(other, tid, claimer=MARK)
                    assert kb.block_task(other, tid, reason=MARK + ' pause', kind='needs_input')
                    if race in {'aba_block', 'recovered_retriaged'}:
                        assert kb.unblock_task(other, tid)
                        assert kb.claim_task(other, tid, claimer=MARK)
                        assert kb.block_task(other, tid, reason=MARK + ' pause', kind='needs_input')
                    if race == 'recovered_retriaged':
                        assert resolve(other, tid, body=MARK + ' NEW obligations C/D')
                        assert kb.claim_task(other, tid, claimer=MARK)
                        assert kb.block_task(other, tid, reason=MARK + ' new pause', kind='needs_input')
            after_race.append(snapshot(other))
    calls = stub_model(monkeypatch, fanout=mode == 'fanout', callback=change_while_model_is_away)
    if mode == 'specify':
        monkeypatch.setattr(ks, '_call_aux', kd._call_aux)
        outcome = ks.specify_task(tid, author=MARK)
    else:
        outcome = kd.decompose_task(tid, author=MARK)
    assert calls == [tid] and not outcome.ok
    assert snapshot(db) == after_race[0]
    assert not db.execute("SELECT 1 FROM task_runs WHERE ended_at IS NULL").fetchone()


@pytest.mark.parametrize('outer_fanout', [False, True])
@pytest.mark.parametrize('winner_fanout', [False, True])
def test_two_inflight_results_have_one_writer_no_children_or_owners_leaked(db, monkeypatch, outer_fanout, winner_fanout):
    from hermes_cli.kanban_db_graph import decompose_triage_task
    tid = create(db, triage=True)
    won = []
    def competing_result():
        with kbc.connect_closing() as other:
            if winner_fanout:
                assert decompose_triage_task(other, tid, root_assignee='synthetic-worker',
                                             children=[{'title': MARK + ' winning child', 'assignee': 'synthetic-worker'}])
            else:
                assert kb.specify_triage_task(other, tid, body=MARK + ' winner')
            won.append(snapshot(other))
    stub_model(monkeypatch, fanout=outer_fanout, callback=competing_result)
    assert not kd.decompose_task(tid, author=MARK).ok
    assert snapshot(db) == won[0]
    assert not db.execute('SELECT 1 FROM task_runs WHERE ended_at IS NULL').fetchone()


def test_mutation_checks_really_run_inside_write_transaction(db, monkeypatch):
    from hermes_cli.kanban_db_graph import decompose_triage_task
    original = kb.triage_specification_allowed
    checks = []
    def observed(conn, tid):
        checks.append(conn.in_transaction)
        return original(conn, tid)
    monkeypatch.setattr(kb, 'triage_specification_allowed', observed)
    assert kb.specify_triage_task(db, create(db, triage=True), body=MARK)
    assert decompose_triage_task(db, create(db, triage=True), root_assignee='synthetic-worker',
                                children=[{'title': MARK}])
    assert checks == [True, True]


@pytest.mark.parametrize('fanout', [False, True])
def test_concurrent_sqlite_writers_only_one_wins(db, fanout):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from hermes_cli.kanban_db_graph import decompose_triage_task
    tid = create(db, triage=True)
    barrier = Barrier(2)
    def writer():
        with kbc.connect_closing() as other:
            barrier.wait(timeout=5)
            if fanout:
                return bool(decompose_triage_task(other, tid, root_assignee='synthetic-worker',
                                                 children=[{'title': MARK + ' concurrent child'}]))
            return kb.specify_triage_task(other, tid, body=MARK + ' concurrent spec')
    # Two tiny competing SQLite transactions, not parallel test execution or workers.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer) for _ in range(2)]
        assert sorted(f.result(timeout=10) for f in futures) == [False, True]
    assert db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == (2 if fanout else 1)
    assert not db.execute('SELECT 1 FROM task_runs').fetchone()
    assert len([event for event in kb.list_events(db, tid)
                if event.kind in {'specified', 'decomposed'}]) == 1
