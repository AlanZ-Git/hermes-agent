"""Notification routing remains same-origin and final-only claims survive reconnects."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


def test_inherited_policy_and_cross_tenant_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "policy.db"))
    with kbc.connect() as conn:
        root = kb.create_task(conn, title="root", tenant="sample", notification_policy="final_only")
        kbn.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="fixture")
        child = kb.create_task(conn, title="child", tenant="sample", parents=[root], notification_policy="inherit")
        other = kb.create_task(conn, title="other", tenant="other", parents=[root], notification_policy="inherit")
        assert kb.get_task(conn, child).notification_policy == "silent"
        assert kb.get_task(conn, other).notification_policy == "direct"
        assert kbn.list_notify_subs(conn, other) == []
        kb.complete_task(conn, root, summary="done")
        old, waiting = kbn.claim_final_only_graph_state(conn, root_id=root, platform="telegram", chat_id="fixture")
        assert not waiting["claimed"]
        assert kb.claim_task(conn, child)
        kb.complete_task(conn, child, summary="done")
        old, claimed = kbn.claim_final_only_graph_state(conn, root_id=root, platform="telegram", chat_id="fixture")
        assert claimed["claimed"] and claimed["all_terminal"] and claimed["descendant_count"] == 1
        _, repeated = kbn.claim_final_only_graph_state(conn, root_id=root, platform="telegram", chat_id="fixture")
        assert not repeated["claimed"]
        assert kbn.rewind_notify_cursor(conn, task_id=root, platform="telegram", chat_id="fixture", claimed_cursor=claimed["cursor"], old_cursor=old)
