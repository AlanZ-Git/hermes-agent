"""Diagnostic wake execution and structured controls survive a presentation veto."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.notification_presentation import notification_turn
from tui_gateway import server


@pytest.mark.parametrize("muted", [False, True])
def test_real_tui_emitter_keeps_control_frames_and_restores_callbacks(monkeypatch, muted):
    frames = []
    monkeypatch.setattr(server, "write_json", lambda frame: frames.append(frame) or True)
    callback = Mock()
    agent = SimpleNamespace(status_callback=callback, clarify_callback=callback)
    with notification_turn(agent, muted=muted, session_id="session"):
        server._emit("message.delta", "session", {"text": "diagnostic echoed by model"})
        server._emit("message.complete", "session", {"text": "diagnostic echoed by model"})
        agent.clarify_callback("question", ["choice"])
        server._emit("notification.clear", "session", {"key": "cleared"})
    assert [frame["params"]["type"] for frame in frames] == (
        ["notification.clear"] if muted else ["message.delta", "message.complete", "notification.clear"])
    assert agent.status_callback is callback
    callback.assert_called_once_with("question", ["choice"])
    server._emit("message.delta", "session", {"text": "next human result"})
    assert frames[-1]["params"]["payload"]["text"] == "next human result"


@pytest.mark.parametrize("suppress", [False, True])
def test_tui_kanban_splits_diagnostics_from_results_before_wake(tmp_path, monkeypatch, suppress):
    import os
    from pathlib import Path
    from hermes_state import SessionDB
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
    from hermes_cli.active_sessions import try_acquire_active_session

    assert tmp_path.resolve().is_relative_to(Path(os.environ['TMPDIR']).resolve())
    owner = tmp_path / 'owner'
    owner.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    (tmp_path / 'config.yaml').write_text(f'display: {{suppress_warning_notifications: {str(not suppress).lower()}}}')
    (owner / 'config.yaml').write_text(f'display: {{suppress_warning_notifications: {str(suppress).lower()}}}')
    with SessionDB(db_path=owner / 'state.db') as db:
        db.create_session('chat', 'tui')
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='requested result')
        kbn.add_notify_sub(conn, task_id=tid, platform='tui', chat_id='chat')
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'crashed', {'reason': 'worker crash'})
            kb._append_event(conn, tid, 'completed', {'summary': 'requested result'})
    lease, refusal = try_acquire_active_session(session_id='chat', surface='tui', config={},
        registry_home=owner, metadata={'live_session_id': 'session'})
    assert refusal is None
    emitted, submitted = [], []
    monkeypatch.setattr(server, '_emit', lambda *args: emitted.append(args))
    def submit(*args, **kwargs):
        submitted.append((args, kwargs))
        kwargs['terminal_callback']({'status': 'settled'})
        return True
    monkeypatch.setattr(server, '_notif_submit', submit)
    session = {'profile_home': str(owner), 'session_key': 'chat', 'active_session_lease': lease,
               'history_lock': threading.RLock()}
    try:
        server._notif_poll_kanban('session', session)
        assert len(submitted) == 1
        assert 'crashed' in submitted[0][0][3]
        assert submitted[0][1]['display_metadata']['notification_category'] == 'diagnostic'
        assert bool(emitted) is not suppress
        session['running'] = False
        emitted.clear()
        server._notif_poll_kanban('session', session)
        assert len(submitted) == 2
        assert 'requested result' in submitted[1][0][3]
        assert not (submitted[1][1].get('display_metadata') or {}).get('notification_category')
        assert emitted
        with SessionDB(db_path=owner / 'state.db') as db:
            receipts = db._read_all('SELECT * FROM kanban_notification_receipts ORDER BY new_cursor')
            assert len(receipts) == 2 and all(r['state'] == 'consumed' for r in receipts)
            first = db._read_one('SELECT * FROM messages WHERE id=?', (receipts[0]['message_row_id'],))
            assert db._decoded_repair_row(first)['display_metadata']['notification_category'] == 'diagnostic'
    finally:
        lease.release()
