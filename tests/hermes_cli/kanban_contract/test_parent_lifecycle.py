"""Independent scope controls; no model call, network, real worker or live profile."""
import json
from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as notify
from hermes_cli import loops
from gateway.kanban_watchers_notifier import _KanbanNotification
from test_triage_boundary import db, create, MARK


def snapshot(conn, tid):
    return tuple(conn.execute('SELECT status,body,current_run_id,claim_lock,worker_pid,consecutive_failures,max_retries FROM tasks WHERE id=?',(tid,)).fetchone())


def test_loop_pause_clear_are_not_kanban_stop(db, monkeypatch):
    memory = {}
    class MetaDB:
        def get_meta(self, key): return memory.get(key)
        def set_meta(self, key, value): memory[key] = value
    monkeypatch.setattr(loops, '_get_session_db', lambda: MetaDB())
    tid = create(db)
    before = snapshot(db, tid)
    manager = loops.LoopManager(MARK + '-session')
    manager.set(MARK + ' monitor only', interval_seconds=60)
    manager.pause()
    assert not manager.is_due(now=10**12)
    assert snapshot(db, tid) == before
    assert manager.clear()
    assert not manager.is_due(now=10**12)
    assert snapshot(db, tid) == before
    # Conversely the independently eligible task can still be claimed.
    assert kb.claim_task(db, tid, claimer=MARK)


def test_comment_is_not_lifecycle_permission_or_stop(db):
    tid = create(db)
    before = snapshot(db, tid)
    kb.add_comment(db, tid, MARK, 'Stop automatic dispatch; historical contract superseded.')
    assert snapshot(db, tid) == before
    # This documents the existing limitation, not desired comment parsing.
    assert kb.claim_task(db, tid, claimer=MARK)
    assert kb.block_task(db, tid, reason=MARK + ' explicit pause', kind='needs_input')
    kb.recompute_ready(db)
    assert kb.get_task(db, tid).status == 'blocked'
    assert kb.claim_task(db, tid, claimer=MARK) is None


def test_late_notification_and_archive_do_not_reopen_task(db):
    tid = create(db)
    key = dict(task_id=tid, platform='telegram', chat_id=MARK)
    notify.add_notify_sub(db, **key, delivery_mode='notify')
    assert kb.claim_task(db, tid, claimer=MARK)
    assert kb.block_task(db, tid, reason=MARK + ' explicit pause', kind='needs_input')
    old, cursor, events = notify.claim_unseen_events_for_sub(db, **key)
    assert any(e.kind == 'blocked' for e in events)
    assert kb.archive_task(db, tid)
    before = snapshot(db, tid)
    # Delayed passive formatter consumes an old blocked event after archive.
    n = _KanbanNotification(SimpleNamespace(), {'sub':dict(key,thread_id='',delivery_mode='notify'),
        'task':kb.get_task(db,tid), 'events':events, 'cursor':cursor, 'old_cursor':old},
        platform_cls=None, sub_fail_counts={})
    assert any(n.format_event(e) for e in events if e.kind == 'blocked')
    notify.advance_notify_cursor(db, **key, new_cursor=cursor)
    assert snapshot(db, tid) == before
    _, _, later = notify.claim_unseen_events_for_sub(db, **key)
    assert any(e.kind == 'archived' for e in later)
    kb.recompute_ready(db)
    assert kb.claim_task(db, tid, claimer=MARK) is None
    assert kb.claim_review_task(db, tid, claimer=MARK) is None
    assert snapshot(db, tid) == before


def test_archive_terminates_owned_worker_at_signal_seam_once(db, monkeypatch):
    tid = create(db)
    claim = kb.claim_task(db, tid, claimer=kb._host_prefix() + 'TODO198-SYNTHETIC')
    assert claim
    with kb.write_txn(db):
        db.execute('UPDATE tasks SET worker_pid=? WHERE id=?', (54321, tid))
    monkeypatch.setattr(kb, '_pid_alive', lambda pid: False)
    signals = []
    assert kb.archive_task(db, tid, signal_fn=lambda pid,sig: signals.append((pid,int(sig))))
    assert signals and signals[0][0] == 54321
    assert not kb.archive_task(db, tid, signal_fn=lambda pid,sig: signals.append((pid,int(sig))))
    assert len(signals) == 1
    row = kb.get_task(db, tid)
    assert row.status == 'archived' and row.current_run_id is None and row.worker_pid is None
    term = db.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='archive_worker_termination'",(tid,)).fetchall()
    assert len(term) == 1 and json.loads(term[0][0])['terminated'] is True
    assert kb.get_run(db, claim.current_run_id).outcome == 'reclaimed'
