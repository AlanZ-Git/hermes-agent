"""Already-applied disposition preserves bytes without re-running a writer."""
import json

import pytest

from tools import pending_history as ph
from tools import steward_skill_execution as se


def test_applied_terminal_history_is_exact_and_recoverable(tmp_path):
    path = tmp_path / 'pending/skills/1234abcd.json'
    path.parent.mkdir(parents=True)
    raw = se.packed({'id': path.stem, 'subsystem': 'skills', 'payload': {'action': 'patch'}}) + b'\n'
    path.write_bytes(raw)
    digest = se.sha(raw)
    binding = {'schema': 'fixture-binding/v1', 'effect_sha256': 'a' * 64}
    receipt = ph.close_applied(tmp_path, path.stem, digest, binding)
    assert receipt['disposition'] == 'applied_closed'
    assert not path.exists()
    assert ph.historical_read(path, path.stem, digest) == raw
    assert ph.close_applied(tmp_path, path.stem, digest, binding) == receipt
    with pytest.raises(ValueError, match='public_disposition_only'):
        ph.discard(tmp_path, path.stem, 'applied_closed')


def test_unconfigured_closeout_cannot_mutate(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    result = json.loads(se.handle({'action': 'closeout-applied', 'source': {}}))
    assert result['success'] is False
    assert result['reason'] == 'host_closeout_inactive'
