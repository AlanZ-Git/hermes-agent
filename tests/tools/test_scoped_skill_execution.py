"""Scoped execution is dormant without host policy; planning grants no authority."""
import json


def test_delegation_absent_and_invalid_is_inactive(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.steward_skill_execution import handle
    result = json.loads(handle({"event_id": "a" * 64}, task_id="cron:fixture:run"))
    assert result["success"] is False and result["reason"] == "host_delegation_inactive"
    (tmp_path / "steward-delegation.json").write_text("{}")
    result = json.loads(handle({"event_id": "a" * 64}, task_id="cron:fixture:run"))
    assert result["success"] is False
