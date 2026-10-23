"""Authorized execution uses native atomic writer and retains exact sources."""
import json

import pytest

from tools import steward_skill_execution as sx
from tools import write_approval as wa


@pytest.mark.platforms("linux")
def test_native_scoped_write_and_dedup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    skill = home / "skills/fixture"
    skill.mkdir(parents=True)
    target = skill / "SKILL.md"
    target.write_text("---\nname: fixture\ndescription: Use when testing scoped writes.\n---\n\nOriginal sentence.\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    state = home / "state"
    (state / "execution-plans").mkdir(parents=True)
    delegation = {"enabled": True, "home": str(home), "state": str(state), "cron_id": "fixture",
                  "pending_roots": [str(home / "pending/skills")], "target_roots": [str(home / "skills")]}
    (home / "steward-delegation.json").write_text(json.dumps(delegation))
    monkeypatch.setattr("tools.approval_context._is_cron_approval_context", lambda: True)
    monkeypatch.setattr(wa, "write_approval_enabled", lambda subsystem: True)
    record = wa.stage_write("skills", {"action": "patch", "name": "fixture",
                            "old_string": "Original sentence.", "new_string": "Revised sentence."},
                            summary="fixture", origin="foreground")
    source = home / "pending/skills" / (record["id"] + ".json")
    source_raw = source.read_bytes()
    digest = sx.sha(source_raw)
    event = "a" * 64
    identity = {"binding": {"source_token": sx.sha(sx.packed(["fixture", record["id"], str(source), digest]))}}
    monkeypatch.setattr(sx, "review_identity", lambda *args: identity)
    targets, _ = sx.plan_targets(record)
    plan = {"schema": "steward-execution-plan/v1", "event_id": event, "identity": identity,
            "decision": "approved", "stage": {"id": record["id"], "sha256": digest}, "targets": targets,
            "sources": [{"scope": "fixture", "path": str(source), "id": record["id"], "sha256": digest,
                         "coverage": [{"ordinal": 0, "operations": [0], "basis": "exact fixture sentence"}]}],
            "outstanding": []}
    sx.store_new(state / "execution-plans" / (event + ".json"), plan)
    result = sx.execute(event, task_id="cron:fixture:run")
    assert result["business_complete"] and result["source_retained"]
    assert "Revised sentence." in target.read_text() and source.read_bytes() == source_raw
    assert sx.execute(event, task_id="cron:fixture:run") == result
    from tools import steward_skill_closeout as closeout
    permission = {"enabled": True, "task_id": "fixture-task", "board": str(tmp_path / "board.db"),
                  "home": str(home), "profile": "fixture", "grant": "fixture-permission",
                  "sources": [["fixture", record["id"], digest]]}
    (home / "steward-delegation.json").write_text(json.dumps(dict(delegation, closeout=permission)))
    monkeypatch.setenv("HERMES_PROFILE", "fixture")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "fixture-task")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "fixture-lock")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    import sqlite3
    with sqlite3.connect(permission["board"]) as conn:
        conn.executescript('CREATE TABLE tasks(id,assignee,status,current_run_id,claim_lock); '
                           'CREATE TABLE task_runs(id,task_id,profile,status,claim_lock);')
        conn.execute('INSERT INTO tasks VALUES(?,?,?,?,?)', ('fixture-task', 'fixture', 'running', 1, 'fixture-lock'))
        conn.execute('INSERT INTO task_runs VALUES(?,?,?,?,?)', (1, 'fixture-task', 'fixture', 'running', 'fixture-lock'))
    request = {"scope": "fixture", "id": record["id"], "sha256": digest,
               "event_id": event, "result_sha256": sx.sha(sx.read(sx.paths(state, event)[1]))}
    assert closeout.closeout(request, dry_run=True)["writes"] == 0
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "wrong")
    with pytest.raises(ValueError, match="live_claim_mismatch"):
        closeout.closeout(request)
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "fixture-lock")
    from agent.delegation_context import delegated_child_context
    with delegated_child_context(), pytest.raises(ValueError, match="dispatcher_owner"):
        closeout.closeout(request)
    from tools import skill_manager_tool as sm
    monkeypatch.setattr(sm, "apply_skill_pending", lambda *a, **kw: pytest.fail("closeout must not write content"))
    from tools import pending_history as ph
    original_record = ph.record_once

    def interrupted(path, value):
        if path.name == 'receipt.json':
            raise OSError('fixture-after-unlink')
        original_record(path, value)

    monkeypatch.setattr(ph, 'record_once', interrupted)
    with pytest.raises(OSError, match='fixture-after-unlink'):
        closeout.closeout(request)
    assert not source.exists()
    with pytest.raises(FileNotFoundError):
        ph.historical_read(source, record['id'], digest)
    monkeypatch.setattr(ph, 'record_once', original_record)
    assert closeout.closeout(request)['status'] == 'applied_closed'
    assert closeout.closeout(request)['status'] == 'applied_closed'
    assert ph.historical_read(source, record['id'], digest) == source_raw
    assert sx.verified_result(state, event)['business_complete']
    with pytest.raises(ValueError, match="original_cron_identity_required"):
        sx.execute(event, task_id="cron:other:run")
    target.write_text(target.read_text() + "drift")
    with pytest.raises(ValueError):
        sx.execute(event, task_id="cron:fixture:run")
