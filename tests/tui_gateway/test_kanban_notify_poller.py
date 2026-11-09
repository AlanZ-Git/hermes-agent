"""Tests for the TUI-side kanban notification poller (issue #59890).

``kanban_create`` auto-subscribes TUI/desktop sessions with
``platform="tui"`` / ``chat_id=HERMES_SESSION_KEY``, but no component ever
read those rows back: the gateway notifier skips them (no "tui" messaging
adapter) and the TUI notification poller only watched process completions.
``last_event_id`` stayed 0 forever and no notification was ever delivered.

These tests cover the delivery half that now lives in tui_gateway/server.py:
``_collect_kanban_notifications`` (cursor claim + formatting + archive-only
unsubscribe) and ``_format_kanban_event_text``.
"""

from types import SimpleNamespace
from unittest.mock import patch

from pathlib import Path
import os

import pytest
from hermes_state import SessionDB
from hermes_cli.active_sessions import try_acquire_active_session
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway.server import (
    _collect_kanban_notifications as _receive_kanban_notifications,
    _format_kanban_event_text,
)

SESSION_KEY = "tui-session-key-1"


_owned_sessions = {}


@pytest.fixture(autouse=True)
def owning_sessions():
    _owned_sessions.clear()
    yield
    for session in _owned_sessions.values():
        session['active_session_lease'].release()
    _owned_sessions.clear()


def _session(key: str = SESSION_KEY, profile_home=None) -> dict:
    home = Path(profile_home or os.environ['HERMES_HOME'])
    assert home.resolve().is_relative_to(Path(os.environ['TMPDIR']).resolve())
    identity = (str(home), key)
    if identity not in _owned_sessions:
        with SessionDB(db_path=home / 'state.db') as db:
            db.create_session(session_id=key, source='tui')
        lease, refusal = try_acquire_active_session(session_id=key, surface='tui', config={},
            registry_home=home, metadata={'live_session_id': 'sid-poller-test'})
        assert refusal is None
        _owned_sessions[identity] = {'session_key': key, 'profile_home': str(home), 'active_session_lease': lease}
    return _owned_sessions[identity]


def _collect_kanban_notifications(session):
    receipts = _receive_kanban_notifications(session)
    if not receipts:
        return []
    with SessionDB(db_path=Path(session['profile_home']) / 'state.db') as db:
        return [db._read_one('SELECT content FROM messages WHERE id=?', (r['message_row_id'],))[0] for r in receipts]


def _pending(session):
    from hermes_state_kanban_notifications import pending
    with SessionDB(db_path=Path(session['profile_home']) / 'state.db') as db:
        return pending(db, session['session_key'])


def _create_subscribed_task(*, chat_id: str = SESSION_KEY, platform: str = "tui"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="notify tui", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id)
        return tid
    finally:
        conn.close()


def _complete(tid: str, summary: str = "all done") -> None:
    conn = kbc.connect()
    try:
        kb.complete_task(conn, tid, summary=summary)
    finally:
        conn.close()


def _sub_rows(tid: str) -> list:
    conn = kbc.connect()
    try:
        return kbn.list_notify_subs(conn, task_id=tid)
    finally:
        conn.close()


class TestCollectKanbanNotifications:
    def test_zero_sub_board_is_never_opened_writable(self):
        conn = kbc.connect()
        conn.close()
        kb.create_board("second-board")

        with patch.object(kbc, "connect", wraps=kbc.connect) as spy_connect:
            texts = _collect_kanban_notifications(_session())

        assert texts == []
        spy_connect.assert_not_called()

    def test_done_reopen_notifies_once_per_event_until_archive(self):
        tid = _create_subscribed_task()
        _complete(tid, summary="shipped the fix")

        first = _collect_kanban_notifications(_session())

        assert len(first) == 1
        assert tid in first[0]
        assert "done" in first[0]
        assert "shipped the fix" in first[0]
        rows = _sub_rows(tid)
        assert len(rows) == 1, "done must retain the originating session"
        first_cursor = rows[0]["last_event_id"]

        # The retained subscription must not replay the completed event.
        assert _collect_kanban_notifications(_session()) == []

        conn = kbc.connect()
        try:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,)
                )
                kb._append_event(conn, tid, "status", {"status": "ready"})
            assert kb.complete_task(conn, tid, summary="review corrections")
        finally:
            conn.close()

        reopened = _collect_kanban_notifications(_session())
        assert len(reopened) == 1
        reopened += _collect_kanban_notifications(_session())

        assert len(reopened) == 2
        assert "ready" in reopened[0]
        assert "review corrections" in reopened[1]
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["chat_id"] == SESSION_KEY
        assert rows[0]["last_event_id"] > first_cursor
        assert _collect_kanban_notifications(_session()) == []

        conn = kbc.connect()
        try:
            assert kb.archive_task(conn, tid)
        finally:
            conn.close()

        final = _collect_kanban_notifications(_session())
        assert len(final) == 1 and 'archived' in final[0]
        assert _collect_kanban_notifications(_session()) == []
        assert _sub_rows(tid)[0]['last_event_id'] > first_cursor

    def test_matching_tui_sub_delivers_and_advances_cursor(self):
        tid = _create_subscribed_task()
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        conn = kbc.connect()
        try:
            kb.block_task(conn, tid, reason="waiting on review")
        finally:
            conn.close()

        with patch.object(kbc, "connect", wraps=kbc.connect) as spy_connect:
            first = _collect_kanban_notifications(_session())
            second = _collect_kanban_notifications(_session())

        assert len(first) == 1
        assert "blocked" in first[0]
        assert "waiting on review" in first[0]
        assert second == []
        assert spy_connect.called
        # Blocked is not a final status -> subscription stays alive so a
        # respawned task's next terminal event still reaches the user.
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["last_event_id"] > pre_cursor

    def test_non_tui_subscription_does_not_open_board_writable(self):
        tid = _create_subscribed_task(platform="telegram", chat_id="chat-1")
        # New subs start caught up at creation time (issue #29905); record the
        # pre-completion cursors so we can assert they were never claimed.
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        _complete(tid)

        with patch.object(kbc, "connect", wraps=kbc.connect) as spy_connect:
            texts = _collect_kanban_notifications(_session())

        assert texts == []
        spy_connect.assert_not_called()
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["last_event_id"] == pre_cursor

    def test_other_tui_session_does_not_open_board_writable(self):
        tid = _create_subscribed_task(chat_id="some-other-session")
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        _complete(tid)

        with patch.object(kbc, "connect", wraps=kbc.connect) as spy_connect:
            texts = _collect_kanban_notifications(_session())

        assert texts == []
        spy_connect.assert_not_called()
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["last_event_id"] == pre_cursor

    def test_probe_error_falls_back_to_writable_delivery(self, monkeypatch):
        tid = _create_subscribed_task()
        _complete(tid, summary="fallback delivery")

        def fail_probe(*args, **kwargs):
            raise OSError("probe unavailable")

        monkeypatch.setattr(kbn, "count_notify_subs", fail_probe)
        with patch.object(kbc, "connect", wraps=kbc.connect) as spy_connect:
            texts = _collect_kanban_notifications(_session())

        assert len(texts) == 1
        assert tid in texts[0]
        spy_connect.assert_called_once()

    def test_no_session_key_is_a_noop(self):
        tid = _create_subscribed_task()
        _complete(tid)

        assert _collect_kanban_notifications({"session_key": ""}) == []
        assert _collect_kanban_notifications({"session_key": None}) == []
        assert len(_sub_rows(tid)) == 1

    def test_profile_scoped_session_reads_the_shared_board(self, tmp_path):
        """The kanban board is shared across profiles BY DESIGN (see the
        hermes_cli/kanban_db.py module docstring): ``kanban_home()`` anchors on
        ``get_default_hermes_root()``, which resolves the process env and
        ignores context-local profile overrides. A Desktop session bound to a
        non-launch profile (``session["profile_home"]``) must therefore still
        have its subscription claimed from the one shared board — the poller
        needs no per-profile home binding.
        """
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        tid = _create_subscribed_task()
        _complete(tid, summary="cross-profile delivery")

        other_profile_home = tmp_path / "profiles" / "reviewer"
        other_profile_home.mkdir(parents=True)
        session = _session(profile_home=other_profile_home)
        # Simulate the strictest case: a context-local profile override is
        # active while the poller collects (as a profile-bound RPC would set).
        token = set_hermes_home_override(str(other_profile_home))
        try:
            texts = _collect_kanban_notifications(session)
        finally:
            reset_hermes_home_override(token)

        assert len(texts) == 1
        assert tid in texts[0]
        assert "cross-profile delivery" in texts[0]
        # Completion is reversible, so the shared-board subscription remains
        # owned by this exact Desktop session until the task is archived.
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["chat_id"] == SESSION_KEY


class TestFormatKanbanEventText:
    SUB = {"task_id": "t_abc123"}
    TASK = SimpleNamespace(title="build the thing", assignee="worker", result=None)

    def test_silent_kinds_return_none(self):
        for kind in ("archived", "unblocked"):
            ev = SimpleNamespace(kind=kind, payload={})
            assert _format_kanban_event_text(self.SUB, self.TASK, ev, "main") is None


    def test_timed_out_with_bad_payload_does_not_raise(self):
        ev = SimpleNamespace(kind="timed_out", payload={"limit_seconds": "not-a-number"})
        text = _format_kanban_event_text(self.SUB, self.TASK, ev, "")
        assert "timed out" in text


class TestNotificationPollerLoopKanbanWiring:
    """Drive a real TUI subscription through ``_notification_poller_loop``.

    Covers the wiring above ``_collect_kanban_notifications``: status.update
    emission, agent-turn dispatch when the session is idle, and the
    busy-session pending buffer that flushes once the session goes idle.
    """

    def _start_poller(self, session: dict, monkeypatch):
        import threading
        from tui_gateway import server

        emits: list = []
        submits: list = []
        monkeypatch.setattr(server, "_KANBAN_POLL_SECONDS", 0.01)
        monkeypatch.setattr(
            server, "_emit", lambda event, sid, payload=None: emits.append((event, payload))
        )
        def accept(rid, sid, sess, text, **kwargs):
            # Model execution is replaced, including its concrete admission receipt.
            server._emit("message.start", sid)
            submits.append(text)
            return True
        monkeypatch.setattr(server, "_run_prompt_submit", accept)
        stop = threading.Event()
        thread = threading.Thread(
            target=server._notification_poller_loop,
            args=(stop, "sid-poller-test", session),
            daemon=True,
        )
        thread.start()
        return stop, thread, emits, submits

    @staticmethod
    def _wait_for(predicate, timeout: float = 5.0) -> bool:
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if predicate():
                return True
            _time.sleep(0.02)
        return False

    def _poller_session(self, *, running: bool = False) -> dict:
        import threading

        session = _session()
        session.update(history_lock=threading.RLock(), running=running)
        return session

    def test_idle_session_gets_status_update_and_agent_turn(self, monkeypatch):
        tid = _create_subscribed_task()
        _complete(tid, summary="poller e2e done")
        session = self._poller_session(running=False)

        stop, thread, emits, submits = self._start_poller(session, monkeypatch)
        try:
            assert self._wait_for(lambda: submits), "agent turn was never dispatched"
        finally:
            stop.set()
            thread.join(timeout=5)

        status_texts = [p["text"] for e, p in emits if e == "status.update" and p]
        assert any(tid in t for t in status_texts), status_texts
        assert any(e == "message.start" for e, _ in emits)
        assert any(tid in text for text in submits), submits
        assert session["running"] is True  # poller claimed the turn
        assert not session.get("_kanban_pending")

    def test_busy_session_buffers_then_flushes_when_idle(self, monkeypatch):
        tid = _create_subscribed_task()
        _complete(tid, summary="buffered while busy")
        session = self._poller_session(running=True)

        stop, thread, emits, submits = self._start_poller(session, monkeypatch)
        try:
            # Busy: the status line appears and the event is buffered, but no
            # agent turn is dispatched while another turn is running.
            assert self._wait_for(
                lambda: any(e == "status.update" for e, _ in emits)
                and _pending(session)
            )
            assert not submits

            with session["history_lock"]:
                session["running"] = False

            assert self._wait_for(lambda: submits), "pending batch never flushed"
        finally:
            stop.set()
            thread.join(timeout=5)

        assert any(tid in text for text in submits), submits
        assert not _pending(session)
        assert session["running"] is True


@pytest.mark.parametrize('outcome', ['refused', 'settled', 'unknown', 'exception', 'running'])
def test_durable_consumption_survives_session_reconstruction(monkeypatch, outcome):
    import threading
    from tui_gateway import server
    tid = _create_subscribed_task()
    _complete(tid, summary='durable across reconstructed session')
    session = _session()
    session.update(history_lock=threading.RLock(), running=False)
    calls = []
    monkeypatch.setattr(server, '_emit', lambda *a, **kw: None)
    def submit(rid, sid, current, text, **kwargs):
        calls.append(text)
        if outcome == 'exception':
            raise RuntimeError('synthetic dispatch result lost')
        if outcome in ('settled', 'unknown'):
            kwargs['terminal_callback']({'status': 'settled' if outcome == 'settled' else 'unknown'})
        return outcome != 'refused'
    monkeypatch.setattr(server, '_run_prompt_submit', submit)
    server._notif_poll_kanban('sid-poller-test', session)
    expected = {'refused': 'pending', 'settled': 'consumed', 'unknown': 'uncertain',
                'exception': 'uncertain', 'running': 'running'}[outcome]
    home = Path(session['profile_home'])
    with SessionDB(db_path=home / 'state.db') as db:
        saved = db._read_one('SELECT * FROM kanban_notification_receipts')
        assert saved['state'] == expected
        assert len(db.get_messages(SESSION_KEY)) == 1
    session['active_session_lease'].release()
    lease, refusal = try_acquire_active_session(session_id=SESSION_KEY, surface='tui', config={},
        registry_home=home, metadata={'live_session_id': 'reconstructed'})
    assert refusal is None
    reconstructed = dict(session_key=SESSION_KEY, profile_home=str(home), active_session_lease=lease,
                         history_lock=threading.RLock(), running=False)
    def finish(rid, sid, current, text, **kwargs):
        calls.append(text)
        kwargs['terminal_callback']({'status': 'settled'})
        return True
    monkeypatch.setattr(server, '_run_prompt_submit', finish)
    try:
        server._notif_poll_kanban('reconstructed', reconstructed)
        assert len(calls) == (2 if outcome == 'refused' else 1)
        with SessionDB(db_path=home / 'state.db') as db:
            saved = db._read_one('SELECT * FROM kanban_notification_receipts')
            assert saved['state'] == ('consumed' if outcome == 'refused' else expected)
            assert len(db.get_messages(SESSION_KEY)) == 1
    finally:
        lease.release()


@pytest.mark.parametrize('queue_key', ['queued_prompt', 'queued_prompts'])
def test_queued_human_input_defers_durable_automatic_consumption(monkeypatch, queue_key):
    import threading
    from tui_gateway import server
    tid = _create_subscribed_task()
    _complete(tid, summary='human goes first')
    session = _session()
    session.update(history_lock=threading.RLock(), running=False)
    session[queue_key] = {'text': 'human request'} if queue_key == 'queued_prompt' else [{'text': 'human request'}]
    calls = []
    monkeypatch.setattr(server, '_emit', lambda *a, **kw: None)
    def submit(rid, sid, current, text, **kwargs):
        calls.append(text)
        kwargs['terminal_callback']({'status': 'settled'})
        return True
    monkeypatch.setattr(server, '_run_prompt_submit', submit)
    server._notif_poll_kanban('sid-poller-test', session)
    assert not calls
    assert len(_pending(session)) == 1
    session.pop(queue_key)
    server._notif_poll_kanban('sid-poller-test', session)
    assert len(calls) == 1 and tid in calls[0]
    assert not _pending(session)


def test_deleted_durable_message_is_not_resurrected_by_poller(monkeypatch):
    import threading
    from tui_gateway import server
    tid = _create_subscribed_task()
    _complete(tid)
    session = _session()
    session.update(history_lock=threading.RLock(), running=True)
    assert _receive_kanban_notifications(session)
    with SessionDB(db_path=Path(session['profile_home']) / 'state.db') as db:
        db._execute_write(lambda c: c.execute('DELETE FROM messages WHERE session_id=?', (SESSION_KEY,)))
    calls, emitted = [], []
    monkeypatch.setattr(server, '_run_prompt_submit', lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(server, '_emit', lambda *args: emitted.append(args))
    session['running'] = False
    server._notif_poll_kanban('sid-poller-test', session)
    assert not calls and not session['running']
    with SessionDB(db_path=Path(session['profile_home']) / 'state.db') as db:
        assert not db.get_messages(SESSION_KEY)
        receipt = db._read_one('SELECT * FROM kanban_notification_receipts')
        assert receipt['reference_state'] == 'orphaned' and receipt['state'] == 'pending'
    assert any('cannot be confirmed' in str(args) for args in emitted)


def _local_consumer_turn(monkeypatch, session):
    import threading
    from tui_gateway import server
    calls = []
    def run(text, **kwargs):
        calls.append(text)
        return {'final_response': 'synthetic local reply'}
    session.update(agent=SimpleNamespace(session_id=SESSION_KEY, run_conversation=run,
                   clear_interrupt=lambda: None), history=[], history_lock=threading.RLock(),
                   history_version=0, running=False, attached_images=[], cols=80, slash_worker=None,
                   show_reasoning=False, tool_progress_mode='all', inflight_turn=None)
    monkeypatch.setattr(server, '_emit', lambda *a, **kw: None)
    for name in ('_wire_callbacks', '_sync_agent_model_with_config', '_register_session_cwd',
                 '_sync_session_key_after_compress'):
        monkeypatch.setattr(server, name, lambda *a, **kw: None)
    monkeypatch.setattr(server, '_session_cwd', lambda s: str(s['profile_home']))
    monkeypatch.setattr(server, '_tts_stream_begin', lambda: None)
    monkeypatch.setattr(server, '_get_usage', lambda agent: {})
    return calls


def _consumer_exit_probe(phase):
    import threading
    from tui_gateway import server
    patcher = pytest.MonkeyPatch()
    session = _session()
    _local_consumer_turn(patcher, session)
    if phase == 'pending':
        assert _receive_kanban_notifications(session)
    elif phase == 'dispatching':
        patcher.setattr(server, '_run_prompt_submit', lambda *a, **kw: os._exit(17))
        server._notif_poll_kanban('child-consumer', session)
        raise AssertionError('dispatch boundary was not reached')
    else:
        entered = threading.Event()
        def run(*a, **kw):
            entered.set()
            threading.Event().wait(60)
            raise AssertionError('synthetic process should have exited')
        session['agent'].run_conversation = run
        server._notif_poll_kanban('child-consumer', session)
        assert entered.wait(10), 'actual accepted submit did not reach local agent'
        with SessionDB(db_path=Path(session['profile_home']) / 'state.db') as db:
            assert db._read_one('SELECT state FROM kanban_notification_receipts')[0] == 'running'
    os._exit(17)


@pytest.mark.parametrize('phase', ['pending', 'dispatching', 'running'])
def test_actual_consumer_process_exit_recovers_without_replay(monkeypatch, phase):
    import subprocess
    import sys
    from tui_gateway import server
    tid = _create_subscribed_task()
    _complete(tid, summary='process exit boundary')
    code = ('from tests.tui_gateway.test_kanban_notify_poller import _consumer_exit_probe; '
            f'_consumer_exit_probe({phase!r})')
    with subprocess.Popen([sys.executable, '-B', '-c', code], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as child:
        try:
            output, error = child.communicate(timeout=35)
            assert child.returncode == 17, (output, error)
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=8)
    session = _session()
    calls = _local_consumer_turn(monkeypatch, session)
    session['queued_prompt'] = {'text': 'human priority'}
    server._notif_poll_kanban('replacement-consumer', session)
    assert not calls
    with SessionDB(db_path=Path(session['profile_home']) / 'state.db') as db:
        receipt = db._read_one('SELECT * FROM kanban_notification_receipts')
        assert receipt['state'] == ('pending' if phase == 'pending' else 'uncertain')
        assert len(db.get_messages(SESSION_KEY)) == 1
        delivery_id = receipt['delivery_id']
    session.pop('queued_prompt')
    server._notif_poll_kanban('replacement-consumer', session)
    if phase == 'pending':
        session['_run_thread'].join(timeout=10)
        assert not session['_run_thread'].is_alive()
    server._notif_poll_kanban('replacement-consumer', session)
    assert len(calls) == (1 if phase == 'pending' else 0)
    with SessionDB(db_path=Path(session['profile_home']) / 'state.db') as db:
        receipt = db._read_one('SELECT * FROM kanban_notification_receipts')
        assert receipt['delivery_id'] == delivery_id
        assert receipt['state'] == ('consumed' if phase == 'pending' else 'uncertain')
        assert len(db.get_messages(SESSION_KEY)) == 1
