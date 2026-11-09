"""Durable Kanban reception and conservative automatic-consumption receipts."""
import hashlib
import json
from pathlib import Path
import time


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kanban_notification_receipts (
    delivery_id TEXT PRIMARY KEY,
    identity_json TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    source_file_json TEXT NOT NULL,
    target_binding TEXT NOT NULL,
    logical_session_id TEXT NOT NULL,
    storage_session_id TEXT NOT NULL,
    old_cursor INTEGER NOT NULL,
    new_cursor INTEGER NOT NULL,
    message_uid TEXT NOT NULL,
    message_row_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','dispatching','running','consumed','uncertain')),
    run_binding TEXT,
    reference_state TEXT NOT NULL DEFAULT 'present',
    error TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS kanban_notification_pending
    ON kanban_notification_receipts(logical_session_id, state, new_cursor);
"""


def validate_schema(conn, *, before_create=False):
    import sqlite3
    table = conn.execute("SELECT sql FROM sqlite_master WHERE name='kanban_notification_receipts'").fetchone()
    if table is None and before_create:
        return
    with sqlite3.connect(':memory:') as expected:
        expected.executescript(SCHEMA_SQL)
        columns = lambda c: [tuple(r) for r in c.execute('PRAGMA table_info(kanban_notification_receipts)')]
        expected_sql = expected.execute("SELECT sql FROM sqlite_master WHERE name='kanban_notification_receipts'").fetchone()[0]
        if (table is None or columns(conn) != columns(expected)
                or ''.join(table[0].split()).lower() != ''.join(expected_sql.split()).lower()):
            raise RuntimeError('Incompatible Kanban notification receipt schema; reception paused')
        index = conn.execute("SELECT sql FROM sqlite_master WHERE name='kanban_notification_pending'").fetchone()
        expected_index = expected.execute("SELECT sql FROM sqlite_master WHERE name='kanban_notification_pending'").fetchone()[0]
        if index is None and before_create:
            return
        if index is None or ''.join(index[0].split()).lower() != ''.join(expected_index.split()).lower():
            raise RuntimeError('Incompatible Kanban notification receipt index; reception paused')


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=isinstance(value, dict))


def delivery_identity(board_path, sub, event):
    path = Path(board_path).expanduser().resolve(strict=True)
    identity = [str(path), sub['task_id'], event.id, event.run_id,
                sub.get('notifier_profile') or '', sub['platform'], sub['chat_id'], sub.get('thread_id') or '']
    raw = canonical_json(identity)
    return 'kanban:' + hashlib.sha256(raw.encode('utf-8')).hexdigest(), raw


def target_binding(db, logical_session_id):
    row = db._read_one("SELECT value FROM state_meta WHERE key='store_instance_id'")
    if row is None or not row[0]:
        raise RuntimeError('Notification receiving store identity unavailable')
    return canonical_json([str(Path(db.db_path).resolve(strict=True)), row[0], logical_session_id])


def accept(db, *, delivery_id, identity_json, source_digest, source_file, binding,
           logical_session_id, storage_session_id, old_cursor, new_cursor, text, display_metadata=None,
           owner_check):
    from agent.message_metadata import stamp_message_timestamp, stamp_message_uid
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from hermes_state_messages import _INSERT_MESSAGE_SQL
    message = stamp_message_timestamp({'role': 'user', 'content': text})
    if display_metadata:
        message['display_metadata'] = display_metadata
    uid = stamp_message_uid(message)
    params = db._message_row_params(storage_session_id, 'user', message, [], message['timestamp'], keep_reasoning=False)
    source_file_json = canonical_json(source_file)
    expected = (identity_json, source_digest, source_file_json, binding, logical_session_id,
                storage_session_id, old_cursor, new_cursor)

    def write(conn):
        owner_check()
        previous = conn.execute('SELECT * FROM kanban_notification_receipts WHERE delivery_id=?', (delivery_id,)).fetchone()
        if previous is not None:
            columns = ('identity_json','source_digest','source_file_json','target_binding','logical_session_id',
                       'storage_session_id','old_cursor','new_cursor')
            if tuple(previous[k] for k in columns) != expected:
                raise RuntimeError('Notification identity conflict; source ACK refused')
            return dict(previous)
        db._check_transcript_write_guards(conn, storage_session_id, None)
        row_id = conn.execute(_INSERT_MESSAGE_SQL, params).lastrowid
        db._bump_session_counters(conn, storage_session_id, 1, 0, unit=True)
        conn.execute('INSERT INTO kanban_notification_receipts '
                     '(delivery_id,identity_json,source_digest,source_file_json,target_binding,logical_session_id,'
                     'storage_session_id,old_cursor,new_cursor,message_uid,message_row_id,state,created_at) '
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                     (delivery_id, *expected, uid, row_id, time.time()))
        return dict(conn.execute('SELECT * FROM kanban_notification_receipts WHERE delivery_id=?', (delivery_id,)).fetchone())
    receipt = db._execute_write(write, patience_s=db._TRANSCRIPT_WRITE_PATIENCE_S)
    if receipt['message_uid'] != uid:
        row = referenced_message(db, receipt)
        if row is None:
            return receipt, None
        message = db._decoded_repair_row(row)
        message['timestamp'] = row['timestamp']
        receipt['message_row_id'] = row['id']
        message.update(_row_id=row['id'], _submit_row_session_id=row['session_id'])
    else:
        message.update(_row_id=receipt['message_row_id'], _submit_row_session_id=storage_session_id)
    message[_DB_PERSISTED_MARKER] = True
    return receipt, message


def consumption_paused(db):
    row = db._read_one("SELECT value FROM state_meta WHERE key='kanban_notification_consumption_paused'")
    return row is not None and row[0] != '0'


def transition(db, delivery_id, expected, state, *, run_binding=None, error=None):
    allowed = {('pending','dispatching'), ('dispatching','pending'), ('dispatching','running'),
               ('dispatching','consumed'), ('dispatching','uncertain'), ('running','consumed'), ('running','uncertain')}
    if (expected, state) not in allowed:
        raise ValueError('Invalid notification consumption transition')
    binding = None if state == 'pending' else run_binding
    return db._execute_write(lambda conn: conn.execute(
        'UPDATE kanban_notification_receipts SET state=?,run_binding=CASE WHEN ? THEN ? ELSE run_binding END,error=? '
        'WHERE delivery_id=? AND state=? AND (? OR ? IS NULL OR run_binding=?)',
        (state, expected == 'pending' or state == 'pending', binding, error, delivery_id, expected,
         expected == 'pending', run_binding, run_binding)).rowcount == 1)


def pending(db, logical_session_id):
    return [dict(row) for row in db._read_all(
        "SELECT * FROM kanban_notification_receipts WHERE logical_session_id=? AND state='pending' ORDER BY created_at,new_cursor",
        (logical_session_id,))]


def referenced_message(db, receipt):
    from hermes_state_compression import _CHAIN_STEP_SQL, _walk_compression_chain

    def resolve(conn):
        chain = _walk_compression_chain(receipt['storage_session_id'],
            lambda current: conn.execute(_CHAIN_STEP_SQL, (current,)).fetchone())
        placeholders = ','.join('?' for _ in chain)
        rows = conn.execute(f'SELECT * FROM messages WHERE message_uid=? AND session_id IN ({placeholders}) ORDER BY id',
                            (receipt['message_uid'], *chain)).fetchall()
        original, latest = (rows[0], rows[-1]) if rows else (None, None)
        if (latest is not None and original['session_id'] == receipt['storage_session_id']
                and latest['active']
                and not latest['_compressed_summary'] and not db._is_model_only_row(latest)
                and latest['role'] == 'user' and latest['content'] == original['content']):
            conn.execute("UPDATE kanban_notification_receipts SET message_row_id=?,"
                         "error=CASE WHEN reference_state='orphaned' THEN NULL ELSE error END,reference_state='present' "
                         'WHERE delivery_id=?', (latest['id'], receipt['delivery_id']))
            return dict(latest)
        conn.execute("UPDATE kanban_notification_receipts SET reference_state='orphaned',error=? WHERE delivery_id=?",
                     ('Received; original message location cannot be confirmed', receipt['delivery_id']))
        return None
    return db._execute_write(resolve)
