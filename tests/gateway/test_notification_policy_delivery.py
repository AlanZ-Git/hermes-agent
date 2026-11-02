"""Notification policies suppress intermediate deliveries, not durable task events."""
from types import SimpleNamespace
from unittest.mock import patch

from gateway.kanban_watchers_notifier import _Collector
from gateway.config import Platform
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


def test_collector_silent_and_final_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "notify.db"))
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: object()}, config=None,
                             _owns_kanban_dispatcher_lock=lambda: True)
    collector = _Collector(runner, kb, notifier_profile="default", gc_due=False, gc_retention_days=30)
    with kbc.connect() as conn:
        root = kb.create_task(conn, title="root", notification_policy="final_only")
        kbn.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="fixture")
        child = kb.create_task(conn, title="child", parents=[root], notification_policy="inherit")
        kb.complete_task(conn, root, summary="done")
        with patch("gateway.kanban_watchers_notifier._adapter_for_subscription", return_value=object()):
            assert collector._claim_for_sub(conn, "default", kbn.list_notify_subs(conn, root)[0]) is None
            kb.claim_task(conn, child)
            kb.complete_task(conn, child, summary="done")
            assert collector._claim_for_sub(conn, "default", kbn.list_notify_subs(conn, child)[0]) is None
            delivery = collector._claim_for_sub(conn, "default", kbn.list_notify_subs(conn, root)[0])
            assert delivery["final_only"] and delivery["events"][0].kind == "completed"
            assert collector._claim_for_sub(conn, "default", kbn.list_notify_subs(conn, root)[0]) is None


def test_collector_final_only_block_uses_selected_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'blocked.db'))
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: object()}, config=None,
                             _owns_kanban_dispatcher_lock=lambda: True)
    collector = _Collector(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30)
    with kbc.connect() as conn:
        root = kb.create_task(conn, title='root', notification_policy='final_only')
        kbn.add_notify_sub(conn, task_id=root, platform='telegram', chat_id='fixture')
        child = kb.create_task(conn, title='child', parents=[root], notification_policy='inherit')
        kb.complete_task(conn, root, summary='done', fire_lifecycle_hook=False)
        kb.block_task(conn, child, kind='needs_input')
        selected = conn.execute("SELECT MAX(id) FROM task_events WHERE kind='blocked'").fetchone()[0]
        kb.add_comment(conn, child, 'fixture', 'audit after block')
        with patch('gateway.kanban_watchers_notifier._adapter_for_subscription', return_value=object()):
            delivery = collector._claim_for_sub(conn, 'default', kbn.list_notify_subs(conn, root)[0])
            assert delivery['events'][0].kind == 'blocked'
            assert delivery['events'][0].id == delivery['cursor'] == selected
            assert kbn.list_notify_subs(conn, root)[0]['last_event_id'] == selected
            assert collector._claim_for_sub(conn, 'default', kbn.list_notify_subs(conn, root)[0]) is None
            assert kbn.rewind_notify_cursor(conn, task_id=root, platform='telegram', chat_id='fixture',
                                            claimed_cursor=selected, old_cursor=delivery['old_cursor'])
            retry = collector._claim_for_sub(conn, 'default', kbn.list_notify_subs(conn, root)[0])
            assert retry['cursor'] == selected
    from gateway.kanban_watchers_notifier import TERMINAL_KINDS, _WAKE_KINDS
    assert not {'notify_origin_changed', 'child_removed'} & set(TERMINAL_KINDS + _WAKE_KINDS)
