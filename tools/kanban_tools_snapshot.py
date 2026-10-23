"""Full task state and triage CAS coordinate share one read snapshot."""

def show_snapshot(kb, conn, tid, fields, task_fields, comment_fields, event_fields, run_fields):
    conn.execute("BEGIN")
    try:
        task = kb.get_task(conn, tid)
        if task is None:
            raise ValueError(f"unknown task {tid}")
        events = kb.list_events(conn, tid)
        return {
            "task": fields(task, task_fields),
            "last_event_id": events[-1].id if events else None,
            "parents": kb.parent_ids(conn, tid),
            "unsatisfied_parents": [{"id": pid, "status": status} for pid, status in kb.unsatisfied_parents(conn, tid)],
            "children": kb.child_ids(conn, tid),
            "comments": [fields(c, comment_fields) for c in kb.list_comments(conn, tid)],
            "events": [fields(e, event_fields) for e in events[-50:]],
            "runs": [fields(r, run_fields) for r in kb.list_runs(conn, tid)],
            "worker_context": kb.build_worker_context(conn, tid),
        }
    finally:
        conn.rollback()
