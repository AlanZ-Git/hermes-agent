"""Notification-policy graph aggregation with durable cursor claims."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def final_only_graph_state(conn: sqlite3.Connection, root_id: str, *, after_event_id: int = 0) -> dict[str, Any]:
    """Only silent same-tenant descendants with the root's exact origin participate."""
    from hermes_cli.kanban_notification_policy import compatible_origin

    root = conn.execute("SELECT tenant, status FROM tasks WHERE id = ?", (root_id,)).fetchone()
    if root is None:
        return {"root_id": root_id, "exists": False, "claimed": False}
    ids = {root_id}
    pending = [root_id]
    while pending:
        parent = pending.pop()
        for row in conn.execute(
            "SELECT t.id, t.notification_policy FROM tasks t JOIN task_links l ON t.id = l.child_id WHERE l.parent_id = ?",
            (parent,),
        ):
            if row["id"] not in ids and row["notification_policy"] == "silent" and compatible_origin(conn, (root_id, row["id"]), root["tenant"]):
                ids.add(row["id"])
                pending.append(row["id"])
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT id, status FROM tasks WHERE id IN ({marks})", tuple(ids)).fetchall()
    cursor = int(conn.execute(f"SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id IN ({marks})", tuple(ids)).fetchone()[0])
    blocked = [row["id"] for row in rows if row["id"] != root_id and row["status"] in {"blocked", "triage"}]
    event = None
    if blocked:
        block_marks = ",".join("?" for _ in blocked)
        event = conn.execute(
            f"SELECT id, payload FROM task_events WHERE task_id IN ({block_marks}) AND kind IN ('blocked','block_loop_detected','gave_up') AND id > ? ORDER BY id DESC LIMIT 1",
            (*blocked, after_event_id),
        ).fetchone()
    return {"root_id": root_id, "exists": True, "cursor": cursor, "changed": cursor > after_event_id,
            "all_terminal": all(row["status"] in {"done", "archived"} for row in rows),
            "descendant_count": len(ids) - 1, "blocking_count": len(blocked),
            "blocking_event_id": int(event["id"]) if event else None,
            "blocking_payload": json.loads(event["payload"] or "{}") if event else {}, "cleanup_task_ids": sorted(ids)}


def claim_final_only_graph_state(conn, *, root_id, platform, chat_id, thread_id=None):
    """Claim the aggregate before delivery; the regular rewind API recovers failure."""
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        row = conn.execute("SELECT last_event_id FROM kanban_notify_subs WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
                           (root_id, platform, chat_id, thread_id or "")).fetchone()
        if row is None:
            return 0, {"exists": False, "claimed": False}
        old = int(row[0])
        state = final_only_graph_state(conn, root_id, after_event_id=old)
        if not ((state.get("all_terminal") and state.get("changed")) or state.get("blocking_event_id")):
            return old, {**state, "claimed": False}
        changed = conn.execute("UPDATE kanban_notify_subs SET last_event_id = ? WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? AND last_event_id = ?",
                               (state["cursor"], root_id, platform, chat_id, thread_id or "", old))
        return old, {**state, "claimed": changed.rowcount == 1}
