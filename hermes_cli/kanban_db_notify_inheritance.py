"""Same-origin subscription inheritance retains every delivery column."""
import time


def inherit_notify_subs(conn, child_id, parents, *, created_at=None):
    from hermes_cli.kanban_notification_policy import compatible_origin

    parent_ids = tuple(dict.fromkeys(p for p in parents if p))
    if not parent_ids:
        return
    child = conn.execute("SELECT tenant FROM tasks WHERE id = ?", (child_id,)).fetchone()
    if child is None or not compatible_origin(conn, parent_ids, child["tenant"]):
        return
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS cursor FROM task_events WHERE task_id = ?", (child_id,),
    ).fetchone()
    cursor = int(row["cursor"] if row is not None else 0)
    placeholders = ",".join("?" * len(parent_ids))
    conn.execute(
        f"""
        INSERT OR IGNORE INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, user_id_alt,
             chat_type, notifier_profile, delivery_mode, delivery_metadata,
             created_at, last_event_id)
        SELECT ?, platform, chat_id, thread_id, user_id, user_id_alt,
               COALESCE(chat_type, 'dm'), notifier_profile,
               COALESCE(delivery_mode, 'notify'), delivery_metadata, ?, ?
          FROM kanban_notify_subs
         WHERE task_id IN ({placeholders})
        """,
        (child_id, int(created_at if created_at is not None else time.time()), cursor, *parent_ids),
    )
