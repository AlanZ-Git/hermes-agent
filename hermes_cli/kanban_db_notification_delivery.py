"""Ordered source ACK after durable target acceptance; no source-side claim queue."""
import hashlib
from pathlib import Path

from hermes_state_kanban_notifications import accept, canonical_json, delivery_identity, target_binding

_BINDING_KEY = 'core_studio_notification_target_v1'
_PAUSED_KEY = 'kanban_notification_consumption_paused'
_AUDIT_KINDS = frozenset({
    'notify_origin_changed', 'commented', 'heartbeat', 'claimed', 'dispatch_intent',
    'spawned', 'worker_registered', 'worker_bound', 'worker_result', 'late_result',
    'owner_admitted', 'owner_launch_intent', 'owner_launch_closed', 'owner_worker_reserved',
    'worker_spawn_refused', 'claim_rejected', 'respawn_guarded', 'skipped_nonspawnable',
    'model_override_set', 'stop_worker_termination', 'terminal_worker_reaped',
    'block_termination_requested', 'block_termination_settled'})


def source_file(conn):
    raw = conn.execute('PRAGMA database_list').fetchone()[2]
    if not raw or not Path(raw).is_absolute():
        raise RuntimeError('Notification source path is not authoritative')
    path = Path(raw).resolve(strict=True)
    stat = path.stat()
    return path, [stat.st_dev, stat.st_ino]


def _binding(conn, key, target, *, bind=False):
    from hermes_cli import kanban_db_notify as notify
    row = conn.execute('SELECT delivery_metadata FROM kanban_notify_subs ' + notify._SUB_KEY_WHERE, key).fetchone()
    if row is None:
        return False
    metadata = notify._decode_notify_delivery_metadata(row[0])
    if metadata.get(_PAUSED_KEY):
        return False
    existing = metadata.get(_BINDING_KEY)
    if existing is None and bind:
        metadata[_BINDING_KEY] = target
        conn.execute('UPDATE kanban_notify_subs SET delivery_metadata=? ' + notify._SUB_KEY_WHERE,
                     (notify._encode_notify_delivery_metadata(metadata), *key))
        return True
    return existing == target


def receive_one(conn, db, sub, *, logical_session_id, storage_session_id, format_event, owner_fence):
    owner_fence.check()
    if (owner_fence.logical_session_id != logical_session_id
            or owner_fence.target_path != Path(db.db_path).resolve(strict=True)
            or db._db_file_identity != owner_fence.target_file_id):
        raise RuntimeError('Notification target does not match admitted owner')
    return _receive_one(conn, db, sub, logical_session_id=logical_session_id,
                        storage_session_id=storage_session_id, format_event=format_event,
                        owner_fence=owner_fence)


def _receive_one(conn, db, sub, *, logical_session_id, storage_session_id, format_event, owner_fence):
    from hermes_cli import kanban_db as kb, kanban_db_notify as notify
    from hermes_state_kanban_notifications import consumption_paused
    if consumption_paused(db):
        return None
    key = notify._sub_key(sub['task_id'], sub['platform'], sub['chat_id'], sub.get('thread_id'))
    binding = target_binding(db, logical_session_id)
    path, file_id = source_file(conn)
    with kb.write_txn(conn):
        owner_fence.check()
        if not _binding(conn, key, binding, bind=True):
            return None
        old = notify._notify_cursor(conn, *key)
        _new, events = notify.unseen_events_for_sub(conn, task_id=key[0], platform=key[1], chat_id=key[2], thread_id=key[3])
        while events and events[0].kind in _AUDIT_KINDS:
            audit = events.pop(0)
            if source_file(conn) != (path, file_id):
                raise RuntimeError('Notification source changed while skipping audit')
            notify._cas_cursor(conn, key, audit.id, old)
            old = audit.id
        if events:
            source = conn.execute('SELECT * FROM task_events WHERE id=? AND task_id=?',
                                  (events[0].id, sub['task_id'])).fetchone()
            digest = hashlib.sha256(canonical_json(dict(source)).encode('utf-8')).hexdigest()
    if not events:
        return None
    event = events[0]
    text = format_event(event)
    if text is None:
        return None
    if text is False:
        with kb.write_txn(conn):
            owner_fence.check()
            if _binding(conn, key, binding) and source_file(conn) == (path, file_id):
                notify._cas_cursor(conn, key, event.id, old)
        return None
    delivery_id, identity_json = delivery_identity(path, sub, event)
    if source_file(conn) != (path, file_id):
        raise RuntimeError('Notification source changed before receive')
    with kb.write_txn(conn):
        owner_fence.check()
        if not _binding(conn, key, binding) or notify._notify_cursor(conn, *key) != old:
            return None
    from gateway.kanban_watchers_notifier import diagnostic_event
    receipt, _message = accept(db, delivery_id=delivery_id, identity_json=identity_json,
        source_digest=digest, source_file=file_id, binding=binding, logical_session_id=logical_session_id,
        storage_session_id=storage_session_id, old_cursor=old, new_cursor=event.id, text=text,
        display_metadata={'notification_category': 'diagnostic'} if diagnostic_event(event) else None,
        owner_check=owner_fence.check)
    if source_file(conn) != (path, file_id):
        raise RuntimeError('Notification source changed before ACK')
    if consumption_paused(db):
        return receipt
    with kb.write_txn(conn):
        owner_fence.check()
        if source_file(conn) != (path, file_id):
            raise RuntimeError('Notification source changed before ACK')
        source = conn.execute('SELECT * FROM task_events WHERE id=? AND task_id=?',
                              (event.id, sub['task_id'])).fetchone()
        if source is None or hashlib.sha256(canonical_json(dict(source)).encode('utf-8')).hexdigest() != digest:
            raise RuntimeError('Notification source event changed; receipt reconciliation required')
        if not _binding(conn, key, binding):
            return receipt
        cursor = notify._notify_cursor(conn, *key)
        if cursor == old:
            notify._cas_cursor(conn, key, event.id, old)
        elif cursor != event.id:
            raise RuntimeError('Notification cursor diverged; receipt reconciliation required')
    return receipt
