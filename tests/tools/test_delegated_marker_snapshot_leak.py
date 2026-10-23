"""Delegated-child identity must not persist in terminal snapshots."""
from __future__ import annotations

import os
import pytest

from agent.delegation_context import delegated_child_context
from tools.environments.local import LocalEnvironment


@pytest.mark.platforms("posix")
def test_delegated_marker_does_not_persist_after_child_context(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    env = LocalEnvironment(cwd=str(tmp_path), timeout=10)
    try:
        with delegated_child_context():
            child = env.execute(
                "printf '%s' \"${HERMES_DELEGATED_CHILD_CONTEXT-unset}\"",
                timeout=10,
            )
        parent = env.execute(
            "printf '%s' \"${HERMES_DELEGATED_CHILD_CONTEXT-unset}\"",
            timeout=10,
        )
        # Upstream now carries the fenced board root rather than a bare flag.
        assert child["output"].strip() not in ("", "unset")
        assert parent["output"].strip() == "unset"
        assert os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") is None
    finally:
        env.cleanup()
