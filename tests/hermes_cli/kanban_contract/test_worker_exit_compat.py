"""TODO175: real, explicitly authorized sys.exit-only children; never Hermes workers."""
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from test_triage_boundary import db, MARK


@pytest.mark.parametrize('code,kind', [(0, 'clean_exit'), (75, 'rate_limited'), (2, 'nonzero_exit')])
def test_synthetic_exit_status_survives_unrelated_popen(db, monkeypatch, tmp_path, code, kind):
    tid = kb.create_task(db, title=MARK + ' TODO175 exit fixture', assignee='default',
                         workspace_kind='dir', workspace_path=str(tmp_path))
    task = kb.claim_task(db, tid)
    assert task
    monkeypatch.setattr(dispatch, '_worker_argv',
                        lambda *args: [sys.executable, '-c', f'import sys; sys.exit({code})'])
    monkeypatch.setattr(dispatch, '_restart_safe_worker_argv', lambda task, cmd: cmd)
    pid = dispatch._default_spawn(task, str(tmp_path))
    assert pid
    deadline = time.monotonic() + 5
    while dispatch._pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not dispatch._pid_alive(pid)
    subprocess.run(['/usr/bin/true'], check=True)
    dispatch.reap_worker_zombies()
    assert dispatch._classify_worker_exit(pid) == (kind, code)
    handles = getattr(dispatch, '_worker_process_handles', getattr(dispatch, '_live_worker_procs', {}))
    assert pid not in handles
    # Synthetic test completes the DB run separately; exit semantics remain with the dispatcher.
    assert kb.block_task(db, tid, expected_run_id=task.current_run_id,
                         kind='needs_input', reason=MARK + ' exit fixture finished')
    assert not db.execute('SELECT 1 FROM task_runs WHERE ended_at IS NULL').fetchone()
