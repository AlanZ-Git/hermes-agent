"""Independent dispatcher integration. Only external spawn/profile/memory sampling are seams."""
from pathlib import Path
import os

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import profiles
from test_triage_boundary import db, create, escalated_review, resolve, stub_model, gateway, MARK


def test_wait_does_not_spawn_but_new_cas_authorization_and_unrelated_work_do(db, monkeypatch):
    wait = escalated_review(db)
    other = create(db)
    transient = create(db)
    assert kb.claim_task(db, transient, claimer=MARK)
    assert kb.block_task(db, transient, reason=MARK + ' retryable', kind='transient')
    # Explicit worker block is sticky even for transient: retain its existing
    # ordinary-unblock contract rather than silently auto-authorizing retries.
    assert kb.unblock_task(db, transient)
    with kb.write_txn(db):
        db.execute('UPDATE tasks SET consecutive_failures=2 WHERE id=?', (wait,))
    runs_before = len(kb.list_runs(db, wait))
    stub_model(monkeypatch)
    monkeypatch.setattr(profiles, 'profile_exists', lambda name: True)
    monkeypatch.setattr(dispatch, '_system_memory_sample', lambda: {})
    monkeypatch.setattr(dispatch, 'review_dispatch_enabled', lambda: True)
    spawns = []
    def spawn(task, workspace, **kwargs):
        # No OS process/PID: this records the exact external-spawn boundary.
        assert Path(workspace).resolve().is_relative_to(Path(os.environ['TODO198_ISOLATION']))
        spawns.append((task.id, task.current_run_id))
        return None
    assert gateway(monkeypatch).auto_decompose_tick(5) == 0
    first = dispatch.dispatch_once(db, spawn_fn=spawn, reconcile_orphans=False)
    assert {t[0] for t in first.spawned} == {other, transient}
    assert wait not in {t[0] for t in spawns}
    assert len(kb.list_runs(db, wait)) == runs_before
    before = kb.get_task(db, wait)
    assert before.status == 'triage' and before.consecutive_failures == 2
    assert resolve(db, wait, body=MARK + ' NEW explicit obligations C/D')
    second = dispatch.dispatch_once(db, spawn_fn=spawn, reconcile_orphans=False)
    assert [t[0] for t in second.spawned] == [wait]
    after = kb.get_task(db, wait)
    assert after.body == MARK + ' NEW explicit obligations C/D'
    assert after.consecutive_failures == 2 and after.block_recurrences == before.block_recurrences
    assert len(kb.list_runs(db, wait)) == runs_before + 1
    assert db.execute("SELECT count(*) FROM task_runs WHERE task_id=? AND ended_at IS NULL",(wait,)).fetchone()[0] == 1
    # Next tick cannot create a second writer for any of the live claims.
    assert not dispatch.dispatch_once(db, spawn_fn=spawn, reconcile_orphans=False).spawned
    assert len(spawns) == 3


def test_dependency_completion_still_promotes_without_touching_human_wait(db):
    wait = escalated_review(db)
    parent = create(db)
    child = create(db, parents=[parent])
    assert kb.get_task(db, child).status == 'todo'
    assert kb.complete_task(db, parent, summary=MARK + ' dependency satisfied')
    assert kb.get_task(db, child).status == 'ready'
    assert kb.claim_task(db, child, claimer=MARK)
    assert kb.get_task(db, wait).status == 'triage'
