import pytest


@pytest.fixture(autouse=True)
def scratch_board(tmp_path, monkeypatch):
    monkeypatch.setenv('TODO199_RUN', str(tmp_path))
    monkeypatch.setenv('TODO198_ISOLATION', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'kanban.db'))
