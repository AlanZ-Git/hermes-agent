"""Triage decisions CAS the observed snapshot and retain failure budgets."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_triage
from hermes_cli.kanban_db_connect import connect


def test_triage_stale_and_exhausted_budget(tmp_path):
    conn = connect(tmp_path / "board.db")
    tid = kb.create_task(conn, title="fixture", assignee="dev", triage=True, max_retries=2)
    conn.execute("UPDATE tasks SET consecutive_failures = 2 WHERE id = ?", (tid,))
    event = kb.list_events(conn, tid)[-1].id
    assert kanban_triage.resolve_triage_task(conn, tid, reason="fixture", expected_event_id=event + 1) is None
    result = kanban_triage.resolve_triage_task(conn, tid, reason="fixture", expected_event_id=event, body="revised")
    assert result["budget_exhausted"] and result["remaining_failures"] == 0
    task = kb.get_task(conn, tid)
    assert task.body == "revised" and task.consecutive_failures == 2
    assert kanban_triage.resolve_triage_task(conn, tid, reason="again", expected_event_id=result["event_id"]) is None
    conn.close()
