"""Compare-and-swap triage adjudication without resetting failure budgets."""


def resolve_triage_task(conn, task_id, *, reason, expected_event_id, body=None, author="kanban_unblock"):
    from hermes_cli import kanban_db as kb
    from agent.redact import redact_sensitive_text

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("triage resolution requires a nonempty reason")
    if type(expected_event_id) is not int or expected_event_id < 1:
        raise ValueError("expected_event_id must be a positive integer")
    if body is not None and not isinstance(body, str):
        raise ValueError("body must be a string")
    with kb.write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        latest = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task_id,)).fetchone()[0]
        if row is None or row["status"] != "triage" or latest != expected_event_id:
            return None
        if any(row[key] is not None for key in ("claim_lock", "claim_expires", "worker_pid", "current_run_id")):
            return None
        occupied = conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id = ? AND (ended_at IS NULL OR status = 'running') LIMIT 1",
            (task_id,),
        ).fetchone()
        if occupied is not None:
            return None
        resume_status = kb._resume_status_from_events(conn, task_id)
        landing = kb._landing_status_after_parents(conn, task_id)
        new_status = resume_status if landing == "ready" else "todo"
        changed = body is not None and body != row["body"]
        conn.execute("UPDATE tasks SET status = ?, body = ? WHERE id = ? AND status = 'triage'",
                     (new_status, body if changed else row["body"], task_id))
        kb._append_event(conn, task_id, "triage_resolved", {
            "status": new_status, "resume_status": resume_status, "from_status": "triage",
            "expected_event_id": expected_event_id, "reason": redact_sensitive_text(reason.strip(), force=True),
            "author": redact_sensitive_text(author, force=True), "body_changed": changed,
        })
        event_id = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task_id,)).fetchone()[0]
        limit = row["max_retries"]
        if limit is None:
            from hermes_cli.config import cfg_get, load_config
            limit = cfg_get(load_config(), "kanban", "failure_limit", default=kb.DEFAULT_FAILURE_LIMIT)
            if limit is None:
                limit = kb.DEFAULT_FAILURE_LIMIT
        remaining = max(0, int(limit) - int(row["consecutive_failures"] or 0))
        return {"status": new_status, "event_id": event_id,
                "budget_exhausted": remaining == 0, "remaining_failures": remaining}


def unblock_with_decision(args, kb, conn, tid, author):
    """Validate the complete call before any ordinary unblock mutation."""
    mode = args.get("resolve_triage", False)
    if type(mode) is not bool:
        raise ValueError("resolve_triage must be a boolean")
    if not mode:
        for key in ("reason", "body"):
            if args.get(key) is not None and not (type(args[key]) is str and args[key] == ""):
                raise ValueError(f"ordinary unblock: omit {key}")
        expected = args.get("expected_event_id")
        if expected is not None and not (type(expected) is int and expected == 0):
            raise ValueError("ordinary unblock: omit expected_event_id")
        if not kb.unblock_task(conn, tid):
            raise ValueError(f"could not unblock {tid} (not blocked or unknown)")
        return {"status": kb.get_task(conn, tid).status}
    result = resolve_triage_task(conn, tid, reason=args.get("reason"),
                                expected_event_id=args.get("expected_event_id"), body=args.get("body"), author=author)
    if result is None:
        raise ValueError("could not resolve triage (unknown, stale, occupied or not triage)")
    actual = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
    if actual is None or actual < result["event_id"]:
        raise ValueError("triage resolution committed but readback failed; stop writing")
    return {**result, "status": kb.get_task(conn, tid).status, "last_event_id": actual}
