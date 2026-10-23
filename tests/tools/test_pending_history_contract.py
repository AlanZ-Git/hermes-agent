"""Rejected skills preserve exact bytes; unknown write attempts cannot replay."""
import hashlib
import json

import pytest

from tools import write_approval as wa
from tools.pending_history import approval_attempt, historical_read


def test_rejected_pending_exact_bytes_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record = wa.stage_write(wa.SKILLS, {"action": "create"}, summary="fixture", origin="foreground")
    path = tmp_path / "pending/skills" / (record["id"] + ".json")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert wa.discard_pending(wa.SKILLS, record["id"])
    assert not path.exists()
    assert historical_read(path, record["id"], digest) == raw
    assert wa.discard_pending(wa.SKILLS, record["id"])


def test_unknown_attempt_blocks_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record = wa.stage_write(wa.SKILLS, {"action": "create"}, summary="fixture", origin="foreground")
    with approval_attempt(tmp_path, record["id"], record):
        pass
    with pytest.raises(ValueError, match="do_not_reapply"):
        with approval_attempt(tmp_path, record["id"], record):
            pytest.fail("must not execute")
