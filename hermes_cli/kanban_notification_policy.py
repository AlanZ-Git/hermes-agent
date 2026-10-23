"""Same-tenant, identical-origin notification inheritance."""


def compatible_origin(conn, parents, tenant):
    parents = tuple(dict.fromkeys(parents))
    if not parents:
        return False
    origins = []
    for parent in parents:
        row = conn.execute("SELECT tenant FROM tasks WHERE id = ?", (parent,)).fetchone()
        if row is None or row["tenant"] != tenant:
            return False
        origins.append(frozenset(tuple(row) for row in conn.execute(
            "SELECT platform, chat_id, thread_id, notifier_profile FROM kanban_notify_subs WHERE task_id = ?",
            (parent,),
        )))
    return bool(origins[0]) and len(set(origins)) == 1


def resolve_policy(conn, parents, tenant, policy):
    if policy not in {"direct", "inherit", "silent", "final_only"}:
        raise ValueError("notification_policy must be direct, inherit, silent, or final_only")
    if policy != "inherit":
        return policy
    if not compatible_origin(conn, parents, tenant):
        return "direct"
    policies = [conn.execute("SELECT notification_policy FROM tasks WHERE id = ?", (parent,)).fetchone()[0]
                for parent in parents]
    return "silent" if any(value in {"silent", "final_only"} for value in policies) else "direct"
