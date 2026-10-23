"""Local notification-policy semantics for Kanban task graphs."""

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


def test_policy_persists_and_explicit_inherit_stays_in_same_origin_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "policy.db"))
    kb.init_db()
    with kbc.connect() as conn:
        root = kb.create_task(
            conn, title="root", assignee="orch", tenant="acme",
            notification_policy="final_only",
        )
        kbn.add_notify_sub(
            conn, task_id=root, platform="telegram", chat_id="chat-1",
            notifier_profile="default",
        )
        child = kb.create_task(
            conn, title="child", assignee="worker", tenant="acme",
            parents=[root], notification_policy="inherit",
        )
        ordinary = kb.create_task(
            conn, title="ordinary", assignee="worker", tenant="acme", parents=[root],
        )

        assert kb.get_task(conn, root).notification_policy == "final_only"
        assert kb.get_task(conn, child).notification_policy == "silent"
        assert kb.get_task(conn, ordinary).notification_policy == "direct"
        inherited = kbn.list_notify_subs(conn, child)
        assert [(s["platform"], s["chat_id"]) for s in inherited] == [("telegram", "chat-1")]
        assert kbn.list_notify_subs(conn, ordinary)[0]["chat_id"] == "chat-1"


def test_inherit_fails_closed_across_tenant_or_ambiguous_origins(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "boundaries.db"))
    kb.init_db()
    with kbc.connect() as conn:
        left = kb.create_task(
            conn, title="left", tenant="a", notification_policy="final_only")
        right = kb.create_task(
            conn, title="right", tenant="a", notification_policy="final_only")
        kbn.add_notify_sub(conn, task_id=left, platform="telegram", chat_id="left")
        kbn.add_notify_sub(conn, task_id=right, platform="telegram", chat_id="right")

        cross_tenant = kb.create_task(
            conn, title="cross", tenant="b", parents=[left], notification_policy="inherit")
        ambiguous = kb.create_task(
            conn, title="ambiguous", tenant="a", parents=[left, right], notification_policy="inherit")

        assert kb.get_task(conn, cross_tenant).notification_policy == "direct"
        assert kbn.list_notify_subs(conn, cross_tenant) == []
        assert kb.get_task(conn, ambiguous).notification_policy == "direct"
        assert kbn.list_notify_subs(conn, ambiguous) == []


def test_final_only_graph_claim_is_durable_and_rewindable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "claim.db"))
    kb.init_db()
    with kbc.connect() as conn:
        root = kb.create_task(conn, title="root", notification_policy="final_only")
        kbn.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="chat")
        child = kb.create_task(
            conn, title="child", parents=[root], notification_policy="inherit")
        assert kb.complete_task(conn, root, summary="root done")
        kb.recompute_ready(conn)

        old_cursor, waiting = kbn.claim_final_only_graph_state(
            conn, root_id=root, platform="telegram", chat_id="chat")
        assert waiting["claimed"] is False
        assert waiting["all_terminal"] is False

        assert kb.claim_task(conn, child) is not None
        assert kb.complete_task(conn, child, summary="child done")
        old_cursor, claimed = kbn.claim_final_only_graph_state(
            conn, root_id=root, platform="telegram", chat_id="chat")
        assert claimed["claimed"] is True
        assert claimed["all_terminal"] is True
        assert claimed["descendant_count"] == 1

        current = kbn.list_notify_subs(conn, root)[0]["last_event_id"]
        assert current == claimed["cursor"]
        assert kbn.rewind_notify_cursor(
            conn, task_id=root, platform="telegram", chat_id="chat",
            claimed_cursor=current, old_cursor=old_cursor,
        ) is True
        assert kbn.list_notify_subs(conn, root)[0]["last_event_id"] == old_cursor


def test_manual_link_inherits_origin_without_joining_final_only_aggregation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "link.db"))
    kb.init_db()
    with kbc.connect() as conn:
        root = kb.create_task(conn, title="root", notification_policy="final_only")
        kbn.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="chat")
        child = kb.create_task(conn, title="standalone")
        kb.link_tasks(conn, root, child)
        assert kb.get_task(conn, child).notification_policy == "direct"
        assert kbn.list_notify_subs(conn, child)[0]["chat_id"] == "chat"
        state = kbn.final_only_graph_state(conn, root)
        assert state["descendant_count"] == 0
