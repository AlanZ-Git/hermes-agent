"""The show snapshot retains its CAS coordinate when another connection commits."""
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from tools import kanban_tools as kt
from tools.kanban_tools_snapshot import show_snapshot
from concurrent.futures import ThreadPoolExecutor
import threading


def test_show_snapshot_keeps_body_and_event_on_same_snapshot(tmp_path, monkeypatch):
    conn = connect(tmp_path / 'board.db')
    tid = kb.create_task(conn, title='fixture', body='before', triage=True)
    before = kb.list_events(conn, tid)[-1].id
    original = kb.list_events
    updated = threading.Event()
    pool = ThreadPoolExecutor(1)
    writes = []

    def concurrent_update():
        other = connect(tmp_path / 'board.db')
        try:
            with kb.write_txn(other):
                other.execute('UPDATE tasks SET body = ? WHERE id = ?', ('after', tid))
                kb._append_event(other, tid, 'commented', {'fixture': True})
                updated.set()
        finally:
            other.close()

    def after_row_read(connection, task_id):
        if not writes:
            writes.append(pool.submit(concurrent_update))
            assert updated.wait(10)
        return original(connection, task_id)

    monkeypatch.setattr(kb, 'list_events', after_row_read)
    try:
        result = show_snapshot(kb, conn, tid, kt._fields, kt._TASK_FIELDS,
                               kt._COMMENT_FIELDS, kt._EVENT_FIELDS, kt._RUN_FIELDS)
        assert result['task']['body'] == 'before'
        assert result['last_event_id'] == before
        writes[0].result(timeout=10)
        task = kb.get_task(conn, tid)
        assert task is not None and task.body == 'after'
    finally:
        conn.close()
        pool.shutdown(wait=True)
