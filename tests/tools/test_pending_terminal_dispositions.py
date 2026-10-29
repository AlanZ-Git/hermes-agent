"""Terminal archives preserve exact source bytes; they never authorize another write."""
import json
from pathlib import Path

import pytest
from tools import pending_history as history
from tools import steward_skill_execution as execution


def pending(tmp_path):
    path = tmp_path / 'pending/skills/1234abcd.json'
    path.parent.mkdir(parents=True)
    raw = json.dumps({'id': '1234abcd', 'subsystem': 'skills', 'payload': {'action': 'patch'}}).encode()
    path.write_bytes(raw)
    return path, raw, execution.sha(raw)


@pytest.mark.parametrize('disposition', ['applied', 'reused', 'mixed', 'superseded'])
def test_terminal_disposition_preserves_source_and_never_recreates_pending(tmp_path, disposition):
    path, raw, digest = pending(tmp_path)
    binding = {'schema': 'steward-source-closeout/v1-cron', 'disposition': disposition,
               'request': {'id': '1234abcd'}, 'synthetic': True}
    receipt = history.close_applied(tmp_path, '1234abcd', digest, binding)
    assert receipt['disposition'] == disposition + '_closed'
    assert not path.exists()
    assert history.historical_read(path, '1234abcd', digest) == raw
    assert history.close_applied(tmp_path, '1234abcd', digest, binding) == receipt
    assert not path.exists()
    with pytest.raises(ValueError):
        history.close_applied(tmp_path, '1234abcd', digest, {**binding, 'synthetic': False})


def test_unknown_public_writer_cannot_be_relabelled_as_adoption(tmp_path):
    path, raw, digest = pending(tmp_path)
    with history.approval_attempt(tmp_path, '1234abcd', json.loads(raw)):
        pass  # An unknown result, NOT a successful content write.
    with pytest.raises(ValueError, match='write_attempt_unknown_do_not_close'):
        history.close_applied(tmp_path, '1234abcd', digest, {'schema': 'synthetic-closeout'})
    assert path.read_bytes() == raw


def test_failed_precommit_preserves_active_source(tmp_path):
    path, raw, digest = pending(tmp_path)
    def reject():
        raise ValueError('synthetic authority changed')
    with pytest.raises(ValueError, match='authority changed'):
        history.close_applied(tmp_path, '1234abcd', digest, {'schema': 'synthetic-closeout'}, precommit=reject)
    assert path.read_bytes() == raw
    _, directory = history.coordinates(path, '1234abcd', digest)
    assert not (directory / 'receipt.json').exists()


def test_terminal_receipt_crash_recovery_requires_identical_binding(tmp_path, monkeypatch):
    path, raw, digest = pending(tmp_path)
    binding = {'schema': 'steward-source-closeout/v1-cron', 'disposition': 'superseded', 'synthetic': True}
    original = history.record_once
    def crash(path, value):
        if Path(path).name == 'receipt.json':
            raise OSError('synthetic crash after source exit')
        return original(path, value)
    monkeypatch.setattr(history, 'record_once', crash)
    with pytest.raises(OSError, match='synthetic crash'):
        history.close_applied(tmp_path, '1234abcd', digest, binding)
    assert not path.exists()
    with pytest.raises(FileNotFoundError):
        history.historical_read(path, '1234abcd', digest)
    monkeypatch.setattr(history, 'record_once', original)
    receipt = history.close_applied(tmp_path, '1234abcd', digest, binding)
    assert receipt['disposition'] == 'superseded_closed'
    assert history.historical_read(path, '1234abcd', digest) == raw


def test_active_drift_is_not_hidden_by_a_valid_archive(tmp_path):
    path, raw, digest = pending(tmp_path)
    history.close_applied(tmp_path, '1234abcd', digest, {'schema': 'synthetic-closeout'})
    path.write_bytes(raw + b' ')
    with pytest.raises(ValueError, match='pending_historical_drift'):
        history.historical_read(path, '1234abcd', digest)
