"""Tests for decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kbc.connect() as conn:
        child_ids = decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
    assert child_ids is not None
    assert len(child_ids) == 2

    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root flipped to todo with orchestrator assignee, gated by children.
    assert root.status == "todo"
    assert root.assignee == "orchestrator"
    # First child has no internal parents → ready on recompute_ready.
    assert c0.status == "ready"
    assert c0.assignee == "researcher"
    # Second child has parents=[0] → stays in todo until c0 completes.
    assert c1.status == "todo"
    assert c1.assignee == "engineer"


def test_decomposed_edge_link_audit_preserves_graph_result(kanban_home):
    from hermes_cli.kanban_db_notify import add_notify_sub
    from hermes_cli.kanban_db_notify_graph import final_only_graph_state

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='silent triage', triage=True, notification_policy='silent')
        add_notify_sub(conn, task_id=tid, platform='tui', chat_id='fixture', notifier_profile='default')
        removed = kb.create_task(conn, title='removed dependency', parents=[tid], notification_policy='inherit')
        assert kb.delete_task(conn, removed)
        child_ids = decompose_triage_task(conn, tid, root_assignee=None, children=[{'title': 'work'}])
        assert child_ids is not None
        parent = child_ids[0]
        assert kb.complete_task(conn, parent, summary='done', fire_lifecycle_hook=False)
        assert kb.complete_task(conn, tid, summary='done', fire_lifecycle_hook=False)
        before = final_only_graph_state(conn, parent)
        assert before['all_terminal'] and before['descendant_count'] == 1
        kb.link_tasks(conn, parent, tid)
        after = final_only_graph_state(conn, parent, after_event_id=before['cursor'])
        assert after['all_terminal'] and after['descendant_count'] == 1
        assert after['cursor'] == before['cursor']
        assert not after['changed']


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kbc.connect() as conn:
        tid = _create_triage(conn)
        child_ids = decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None

    with kbc.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    assert comments
    assert any(ev.kind == "decomposed" for ev in events)




