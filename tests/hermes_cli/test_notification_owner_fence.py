"""Profile owner exclusion spans the real source read, target commit and ACK."""
import multiprocessing
import os
from pathlib import Path
import threading

import pytest

from hermes_cli import active_sessions as owners
from hermes_cli import kanban_db as kb, kanban_db_connect as connect, kanban_db_notify as notify
from hermes_cli.kanban_db_notification_delivery import receive_one
from hermes_state import SessionDB


def _contender(home, board, target, out):
    lease, refusal = owners.try_acquire_active_session(session_id='chat', surface='studio-test', config={},
        registry_home=home, metadata={'live_session_id': 'other-live'})
    if lease is None:
        out.send(('refused', refusal.reason))
        return
    try:
        with SessionDB(db_path=target) as db, connect.connect(db_path=board) as conn:
            db.create_session('chat', 'cli')
            with owners.notification_owner_fence(lease=lease, logical_session_id='chat',
                    live_session_id='other-live', profile_home=home, resolve_target=lambda: target) as fence:
                receipt = receive_one(conn, db, notify.list_notify_subs(conn)[0], logical_session_id='chat',
                    storage_session_id='chat', format_event=lambda event: 'test delivery', owner_fence=fence)
                out.send(('received', bool(receipt)))
    finally:
        lease.release()


@pytest.fixture
def owned(tmp_path, monkeypatch):
    assert tmp_path.resolve().is_relative_to(Path(os.environ['TMPDIR']).resolve())
    home = tmp_path / 'owner'
    home.mkdir()
    target = home / 'state.db'
    board = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(board))
    with SessionDB(db_path=target) as db, connect.connect(db_path=board) as conn:
        db.create_session('chat', 'cli')
        tid = kb.create_task(conn, title='isolated owner notification')
        notify.add_notify_sub(conn, task_id=tid, platform='tui', chat_id='chat')
        notify.advance_notify_cursor(conn, task_id=tid, platform='tui', chat_id='chat',
            new_cursor=conn.execute('SELECT MAX(id) FROM task_events').fetchone()[0])
        sub = notify.list_notify_subs(conn)[0]
        kb._append_event(conn, tid, 'blocked', {'reason': 'test'})
        conn.commit()
        lease, refusal = owners.try_acquire_active_session(session_id='chat', surface='core-test', config={},
            registry_home=home, metadata={'live_session_id': 'live'})
        assert refusal is None
        try:
            yield home, target, board, db, conn, sub, lease
        finally:
            lease.release()


def _fence(home, target, lease, **changes):
    args = dict(lease=lease, logical_session_id='chat', live_session_id='live',
                profile_home=home, resolve_target=lambda: target)
    args.update(changes)
    return owners.notification_owner_fence(**args)


@pytest.mark.platforms('linux')
def test_two_processes_different_target_only_owner_commits_and_acks(owned):
    home, target, board, db, conn, sub, lease = owned
    ctx = multiprocessing.get_context('fork')
    parent, child = ctx.Pipe(duplex=False)
    other = home / 'studio.db'
    process = ctx.Process(target=_contender, args=(home, board, other, child))
    process.start()
    try:
        assert parent.poll(8)
        assert parent.recv() == ('refused', owners.SESSION_NOT_OWNED)
        process.join(8)
        assert process.exitcode == 0
        assert not other.exists()
        assert notify._notify_cursor(conn, sub['task_id'], 'tui', 'chat', '') == sub['last_event_id']
        with _fence(home, target, lease) as fence:
            result = receive_one(conn, db, sub, logical_session_id='chat', storage_session_id='chat',
                format_event=lambda event: 'test delivery', owner_fence=fence)
        assert len(db.get_messages('chat')) == 1
        assert notify._notify_cursor(conn, sub['task_id'], 'tui', 'chat', '') == result['new_cursor']
    finally:
        if process.is_alive():
            process.terminate()
            process.join(8)
        parent.close()
        child.close()


@pytest.mark.parametrize('operation', ['release', 'transfer'])
def test_fence_excludes_owner_change_through_commit_and_ack(owned, operation):
    home, target, board, db, conn, sub, lease = owned
    started, finished = threading.Event(), threading.Event()
    results = []
    def change():
        started.set()
        results.append(lease.release() if operation == 'release' else
                       owners.transfer_active_session(lease, session_id='different'))
        finished.set()
    with _fence(home, target, lease) as fence:
        thread = threading.Thread(target=change)
        thread.start()
        assert started.wait(8)
        assert not finished.is_set()
        result = receive_one(conn, db, sub, logical_session_id='chat', storage_session_id='chat',
            format_event=lambda event: 'test delivery', owner_fence=fence)
        assert result and not finished.is_set()
        fence.check()
    thread.join(8)
    assert finished.is_set()
    with pytest.raises(owners.ActiveSessionRegistryError):
        with _fence(home, target, lease):
            pass


@pytest.mark.parametrize('bad', ['live', 'profile', 'released', 'target'])
def test_wrong_owner_or_target_never_reads_or_acks(owned, bad):
    home, target, board, db, conn, sub, lease = owned
    with _fence(home, target, lease):
        pass
    changes = {}
    if bad == 'live':
        changes['live_session_id'] = 'foreign'
    elif bad == 'profile':
        changes['profile_home'] = home.parent
    elif bad == 'released':
        lease.release()
    else:
        other = home / 'other.db'
        other.touch()
        changes['resolve_target'] = lambda: other
    with pytest.raises(owners.ActiveSessionRegistryError):
        with _fence(home, target, lease, **changes):
            pytest.fail('untrusted owner admitted')
    assert not db.get_messages('chat')
    assert notify._notify_cursor(conn, sub['task_id'], 'tui', 'chat', '') == sub['last_event_id']


def test_replaced_target_refuses_commit_and_ack(owned):
    home, target, board, db, conn, sub, lease = owned
    with _fence(home, target, lease) as fence:
        replacement = home / 'replacement.db'
        replacement.touch()
        replacement.replace(target)
        with pytest.raises(owners.ActiveSessionRegistryError, match='database changed'):
            receive_one(conn, db, sub, logical_session_id='chat', storage_session_id='chat',
                format_event=lambda event: 'test delivery', owner_fence=fence)
    assert notify._notify_cursor(conn, sub['task_id'], 'tui', 'chat', '') == sub['last_event_id']


def test_transfer_cannot_overwrite_another_live_session(owned):
    home, target, board, db, conn, sub, lease = owned
    other, refusal = owners.try_acquire_active_session(session_id='other', surface='core-test', config={},
        registry_home=home, metadata={'live_session_id': 'other'})
    assert refusal is None
    try:
        assert owners.transfer_active_session(lease, session_id='other') is False
        with _fence(home, target, lease) as fence:
            fence.check()
    finally:
        other.release()
