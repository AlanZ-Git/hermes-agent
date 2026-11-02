"""Notification-policy graph aggregation from existing lifecycle and origin facts."""
from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from typing import Any


@contextmanager
def notification_snapshot(conn):
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.execute("ROLLBACK")


def _members(root_id, tasks, origins, edges):
    root = tasks[root_id]
    origin = origins.get(root_id, set())
    ids = {root_id}
    pending = [root_id]
    while pending:
        parent = pending.pop()
        for child in edges.get(parent, ()):
            task = tasks.get(child)
            if (child not in ids and task and task['notification_policy'] == 'silent'
                    and task['tenant'] == root['tenant'] and origin
                    and origins.get(child, set()) == origin):
                ids.add(child)
                pending.append(child)
    return ids


def _result_cursor(conn, root_id, tasks, origins, edges, ids):
    events = conn.execute("SELECT id, task_id, kind, payload FROM task_events WHERE kind IN "
                          "('completed','linked','unlinked','created','archived','notify_origin_changed','child_removed',"
                          "'status','claimed','blocked','block_loop_detected','gave_up','unblocked','descendant_invalidated',"
                          "'review_reopened','review_requested','changes_requested','scheduled','decomposed') ORDER BY id").fetchall()
    active = set()
    effective_links = set()
    terminal = {}
    unfinished_archives = set()
    for event in events:
        payload = json.loads(event['payload'] or '{}')
        tid, kind = event['task_id'], event['kind']
        if kind == 'archived':
            previous = payload.get('previous_status')
            if (previous is not None and previous not in {'done', 'archived'}) or (previous is None and terminal.get(tid) is False):
                unfinished_archives.add(event['id'])
            terminal[tid] = True
        elif kind == 'completed':
            terminal[tid] = True
        elif kind in {'status', 'created'}:
            if payload.get('status') is not None:
                terminal[tid] = payload['status'] in {'done', 'archived'}
        elif kind in {'claimed', 'blocked', 'block_loop_detected', 'gave_up', 'unblocked', 'descendant_invalidated',
                      'review_reopened', 'review_requested', 'changes_requested', 'scheduled', 'decomposed'}:
            terminal[tid] = False
        edge = (payload.get('parent'), payload.get('child', event['task_id']))
        if event['kind'] == 'created':
            active.update((parent, event['task_id']) for parent in payload.get('parents', ()))
        elif event['kind'] == 'decomposed':
            active.update((child, tid) for child in payload.get('child_ids', ()))
        elif event['kind'] == 'linked':
            if edge not in active:
                effective_links.add(event['id'])
                active.add(edge)
        elif event['kind'] in {'unlinked', 'child_removed'}:
            active.discard(edge)
    # Undo membership deltas: the newest qualifying fact may belong to an excluded child.
    for event in reversed(events):
        kind, tid = event['kind'], event['task_id']
        payload = json.loads(event['payload'] or '{}')
        if kind == 'completed' and tid in ids:
            return int(event['id'])
        if kind == 'archived' and tid in ids and event['id'] in unfinished_archives:
            return int(event['id'])
        before = ids
        if kind == 'notify_origin_changed':
            origin = origins.setdefault(tid, set())
            origin.difference_update(tuple(value) for value in payload['added'])
            origin.update(tuple(value) for value in payload['removed'])
        elif kind == 'linked' and event['id'] in effective_links:
            if payload['parent'] in ids and payload.get('child', tid) in ids:
                return int(event['id'])
            edges.setdefault(payload['parent'], set()).discard(payload.get('child', tid))
        elif kind in {'unlinked', 'child_removed'}:
            child = payload.get('child', tid)
            if kind == 'child_removed':
                tasks[child] = payload['task']
                origins[child] = {tuple(value) for value in payload['origins']}
            edges.setdefault(payload['parent'], set()).add(child)
        elif kind == 'created':
            for parent in payload.get('parents', ()):
                edges.setdefault(parent, set()).discard(tid)
        elif kind == 'decomposed':
            for child in payload.get('child_ids', ()):
                edges.setdefault(child, set()).discard(tid)
        else:
            continue
        ids = _members(root_id, tasks, origins, edges)
        if ids != before:
            return int(event['id'])
    return 0


def final_only_graph_state(conn: sqlite3.Connection, root_id: str, *, after_event_id: int = 0) -> dict[str, Any]:
    with notification_snapshot(conn):
        tasks = {row['id']: dict(row) for row in conn.execute(
            "SELECT id, tenant, status, notification_policy FROM tasks")}
        if root_id not in tasks:
            return {"root_id": root_id, "exists": False, "claimed": False}
        origins = {}
        for row in conn.execute("SELECT task_id, platform, chat_id, thread_id, notifier_profile FROM kanban_notify_subs"):
            origins.setdefault(row['task_id'], set()).add(tuple(row)[1:])
        edges = {}
        for row in conn.execute("SELECT parent_id, child_id FROM task_links"):
            edges.setdefault(row['parent_id'], set()).add(row['child_id'])
        ids = _members(root_id, tasks, origins, edges)
        blocked = sorted(tid for tid in ids if tid != root_id and tasks[tid]['status'] in {'blocked', 'triage'})
        all_terminal = all(tasks[tid]['status'] in {'done', 'archived'} for tid in ids)
        event = None
        if blocked:
            marks = ','.join('?' for _ in blocked)
            event = conn.execute(
                f"SELECT id, payload FROM task_events WHERE task_id IN ({marks}) AND kind IN ('blocked','block_loop_detected','gave_up') AND id > ? ORDER BY id DESC LIMIT 1",
                (*blocked, after_event_id),
            ).fetchone()
        cursor = _result_cursor(conn, root_id, tasks, origins, edges, ids)
        return {"root_id": root_id, "exists": True, "cursor": cursor, "changed": cursor > after_event_id,
                "all_terminal": all_terminal, "descendant_count": len(ids) - 1, "blocking_count": len(blocked),
                "blocking_event_id": int(event['id']) if event else None,
                "blocking_payload": json.loads(event['payload'] or '{}') if event else {}, "cleanup_task_ids": sorted(ids)}


def claim_final_only_graph_state(conn, *, root_id, platform, chat_id, thread_id=None):
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn, allow_nested=True):
        row = conn.execute("SELECT last_event_id FROM kanban_notify_subs WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
                           (root_id, platform, chat_id, thread_id or "")).fetchone()
        if row is None:
            return 0, {"exists": False, "claimed": False}
        old = int(row[0])
        state = final_only_graph_state(conn, root_id, after_event_id=old)
        if state.get('all_terminal') and state.get('changed'):
            cursor = state['cursor']
        elif state.get('blocking_event_id'):
            cursor = state['blocking_event_id']
        else:
            return old, {**state, "claimed": False}
        changed = conn.execute("UPDATE kanban_notify_subs SET last_event_id = ? WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? AND last_event_id = ?",
                               (cursor, root_id, platform, chat_id, thread_id or "", old))
        return old, {**state, "cursor": cursor, "claimed": changed.rowcount == 1}
