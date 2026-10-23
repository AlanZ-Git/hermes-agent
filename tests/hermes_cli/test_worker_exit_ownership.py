"""The Kanban dispatcher remains the owner of worker exit status."""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from tests.hermes_cli.test_kanban_worker_spawn_toolsets import _make_task
from tests.waiting import wait_until


@pytest.mark.parametrize(
    "code,kind",
    [(0, "clean_exit"), (75, "rate_limited"), (2, "nonzero_exit")],
)
@pytest.mark.platforms("linux")
def test_spawn_retains_exit_after_unrelated_popen(monkeypatch, tmp_path, code, kind):
    home = tmp_path / "home"
    home.mkdir()
    home.joinpath("config.yaml").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        dispatch,
        "_worker_argv",
        lambda *args: [sys.executable, "-c", f"import sys; sys.exit({code})"],
    )
    monkeypatch.setattr(dispatch, "_restart_safe_worker_argv", lambda task, cmd: cmd)

    pid = dispatch._default_spawn(_make_task(kb, assignee="default"), str(tmp_path))
    assert pid is not None
    assert wait_until(lambda: not dispatch._pid_alive(pid), timeout=10)
    assert not dispatch._pid_alive(pid)

    subprocess.run(["/usr/bin/true"], check=True)
    dispatch.reap_worker_zombies()

    assert dispatch._classify_worker_exit(pid) == (kind, code)
    assert pid not in dispatch._live_worker_procs
