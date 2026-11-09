"""Source ACK follows atomic visible-message acceptance, including crash recovery."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_kanban_notifications import pending, referenced_message, transition
from hermes_cli import kanban_db as kb, kanban_db_connect as connect, kanban_db_notify as notify
from hermes_cli.kanban_db_notification_delivery import receive_one


@pytest.fixture
def stores(tmp_path, monkeypatch):
    assert tmp_path.resolve().is_relative_to(Path(os.environ['TMPDIR']).resolve())
    path = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    with connect.connect(db_path=path) as conn, SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('chat', 'cli')
        tid = kb.create_task(conn, title='synthetic notification')
        notify.add_notify_sub(conn, task_id=tid, platform='tui', chat_id='chat')
        notify.advance_notify_cursor(conn, task_id=tid, platform='tui', chat_id='chat',
                                     new_cursor=conn.execute('SELECT MAX(id) FROM task_events').fetchone()[0])
        sub = notify.list_notify_subs(conn, task_id=tid)[0]
        kb._append_event(conn, tid, 'blocked', {'reason': 'synthetic'})
        conn.commit()
        yield conn, db, sub


def receive(conn, db, sub):
    from hermes_cli.active_sessions import notification_owner_fence, try_acquire_active_session
    home = Path(db.db_path).parent
    lease, refusal = try_acquire_active_session(session_id='chat', surface='test', config={},
        registry_home=home, metadata={'live_session_id': 'receipt-test'})
    assert refusal is None
    try:
        with notification_owner_fence(lease=lease, logical_session_id='chat',
                live_session_id='receipt-test', profile_home=home,
                resolve_target=lambda: db.db_path) as fence:
            return receive_one(conn, db, sub, logical_session_id='chat', storage_session_id='chat',
                               format_event=lambda event: 'synthetic notification', owner_fence=fence)
    finally:
        lease.release()


def test_message_receipt_and_counter_commit_before_ack(stores, monkeypatch):
    conn, db, sub = stores
    original = notify._cas_cursor
    def check(conn, key, new, old):
        rows = db.get_messages('chat')
        assert len(rows) == 1 and rows[0]['content'] == 'synthetic notification'
        assert db.get_session('chat')['message_count'] == 1
        receipt = pending(db, 'chat')[0]
        assert receipt['message_row_id'] == rows[0]['id']
        assert receipt['new_cursor'] == new
        return original(conn, key, new, old)
    monkeypatch.setattr(notify, '_cas_cursor', check)
    result = receive(conn, db, sub)
    identity = [str(Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve()),
                sub['task_id'], result['new_cursor'], None, '', 'tui', 'chat', '']
    assert result['delivery_id'] == 'kanban:' + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    assert not receive(conn, db, sub)


def test_saved_before_ack_crash_reuses_receipt(stores, monkeypatch):
    conn, db, sub = stores
    original = notify._cas_cursor
    def crash(*args):
        raise RuntimeError('synthetic crash after receiving commit')
    monkeypatch.setattr(notify, '_cas_cursor', crash)
    with pytest.raises(RuntimeError, match='synthetic crash'):
        receive(conn, db, sub)
    assert notify._notify_cursor(conn, sub['task_id'], 'tui', 'chat', '') == sub['last_event_id']
    assert len(db.get_messages('chat')) == 1
    monkeypatch.setattr(notify, '_cas_cursor', original)
    assert receive(conn, db, sub)['delivery_id'] == pending(db, 'chat')[0]['delivery_id']
    assert len(db.get_messages('chat')) == 1
    assert db.get_session('chat')['message_count'] == 1


def test_receipt_failure_rolls_back_message_and_counter(stores):
    conn, db, sub = stores
    db._execute_write(lambda c: c.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON kanban_notification_receipts "
                                          "BEGIN SELECT RAISE(ABORT,'synthetic rejection'); END"))
    with pytest.raises(sqlite3.IntegrityError, match='synthetic rejection'):
        receive(conn, db, sub)
    assert not db.get_messages('chat')
    assert db.get_session('chat')['message_count'] == 0
    assert notify._notify_cursor(conn, sub['task_id'], 'tui', 'chat', '') == sub['last_event_id']


def test_different_target_store_cannot_accept_same_subscription(stores, tmp_path):
    conn, db, sub = stores
    receive(conn, db, sub)
    kb._append_event(conn, sub['task_id'], 'completed', {})
    conn.commit()
    with SessionDB(db_path=tmp_path / 'other-state.db') as other:
        other.create_session('chat', 'cli')
        assert receive(conn, other, sub) is None
        assert not other.get_messages('chat')
    assert receive(conn, db, sub)


def test_unknown_dispatch_and_deleted_reference_never_replay(stores):
    conn, db, sub = stores
    receipt = receive(conn, db, sub)
    assert transition(db, receipt['delivery_id'], 'pending', 'dispatching', run_binding='exact-run')
    assert transition(db, receipt['delivery_id'], 'dispatching', 'uncertain', error='dispatch outcome unavailable')
    assert not pending(db, 'chat')
    db._execute_write(lambda c: c.execute('DELETE FROM messages WHERE id=?', (receipt['message_row_id'],)))
    assert referenced_message(db, receipt) is None
    assert not db.get_messages('chat')
    saved = db._read_one('SELECT reference_state,error FROM kanban_notification_receipts WHERE delivery_id=?',
                         (receipt['delivery_id'],))
    assert saved['reference_state'] == 'orphaned'
    assert 'cannot be confirmed' in saved['error']


@pytest.mark.parametrize('rotation', ['in_place', 'child'])
def test_exact_uid_compaction_reference_is_repaired_without_duplicate(stores, rotation):
    conn, db, sub = stores
    receipt = receive(conn, db, sub)
    original = db._read_one('SELECT * FROM messages WHERE id=?', (receipt['message_row_id'],))
    carried = db._decoded_repair_row(original)
    carried['timestamp'] = original['timestamp']
    holder = f'pid={os.getpid()}:cmp=notification'
    assert db.try_acquire_compression_lock('chat', holder)
    try:
        if rotation == 'in_place':
            db.archive_and_compact('chat', [carried], tail_count=1, lock_holder=holder)
        else:
            db.publish_compression_child(parent_session_id='chat', child_session_id='child', source='cli',
                messages=[carried], compression_lock_holder=holder)
    finally:
        db.release_compression_lock('chat', holder)
    mapped = referenced_message(db, receipt)
    assert mapped['message_uid'] == receipt['message_uid']
    assert mapped['id'] != receipt['message_row_id']
    assert mapped['session_id'] == ('child' if rotation == 'child' else 'chat')
    saved = db._read_one('SELECT * FROM kanban_notification_receipts WHERE delivery_id=?', (receipt['delivery_id'],))
    assert saved['message_row_id'] == mapped['id'] and saved['reference_state'] == 'present'
    assert saved['storage_session_id'] == 'chat'
    assert not receive(conn, db, sub)
    db._execute_write(lambda c: c.execute('UPDATE messages SET active=0,compacted=0 WHERE id=?', (mapped['id'],)))
    assert referenced_message(db, receipt) is None
    assert not db._read_one('SELECT active FROM messages WHERE id=?', (mapped['id'],))['active']


@pytest.mark.parametrize('replacement', ['summary', 'absorbed', 'same_text', 'foreign_uid'])
def test_summary_text_or_foreign_uid_is_not_a_complete_reference(stores, replacement):
    conn, db, sub = stores
    receipt = receive(conn, db, sub)
    original = db._read_one('SELECT * FROM messages WHERE id=?', (receipt['message_row_id'],))
    if replacement == 'foreign_uid':
        db.create_session('foreign', 'cli')
        db.append_message('foreign', 'user', original['content'], message_uid=receipt['message_uid'])
        db._execute_write(lambda c: c.execute('DELETE FROM messages WHERE id=?', (original['id'],)))
    else:
        from agent.message_metadata import ABSORBED_MESSAGE_UIDS
        message = {'role': 'user', 'content': original['content']}
        if replacement == 'summary':
            message.update(message_uid=receipt['message_uid'], _compressed_summary=True, content='summary only')
        elif replacement == 'absorbed':
            message.update({ABSORBED_MESSAGE_UIDS: [receipt['message_uid']], '_compressed_summary': True})
        db.archive_and_compact('chat', [message])
    assert referenced_message(db, receipt) is None
    saved = db._read_one('SELECT reference_state,error FROM kanban_notification_receipts WHERE delivery_id=?',
                         (receipt['delivery_id'],))
    assert saved['reference_state'] == 'orphaned'
    assert 'cannot be confirmed' in saved['error']


def test_preopened_target_handle_cannot_receive_into_replaced_database(stores):
    conn, db, sub = stores
    path = Path(db.db_path)
    path.rename(path.with_name('detached-state.db'))
    with SessionDB(db_path=path) as replacement:
        replacement.create_session('chat', 'cli')
        with pytest.raises(RuntimeError, match='target does not match admitted owner'):
            receive(conn, db, sub)
        assert not replacement.get_messages('chat')
    assert notify._notify_cursor(conn, sub['task_id'], 'tui', 'chat', '') == sub['last_event_id']
