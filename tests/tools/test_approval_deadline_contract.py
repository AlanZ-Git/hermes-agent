"""Expired requests cannot grant permission even while notifier delivery is slow."""
from tools import approval
from tools import approval_gateway_wait as wait


def test_expired_request_cannot_resolve(monkeypatch):
    entry = wait._ApprovalEntry({"command": "fixture", "pattern_keys": []})
    monkeypatch.setattr(wait.time, "monotonic", lambda: entry.deadline + 1)
    with approval._lock:
        approval._gateway_queues["fixture-deadline"] = [entry]
    try:
        assert approval.resolve_gateway_approval("fixture-deadline", "once") == 0
        assert entry.result is None
    finally:
        with approval._lock:
            approval._gateway_queues.pop("fixture-deadline", None)


def test_execute_error_keeps_context_not_raw_command():
    import json
    from tools.code_execution_tool import _error_result
    result = json.loads(_error_result("blocked", approval_result={
        "request_id": "fixture", "outcome": "timeout", "command": "private", "status": "success",
    }))
    assert result["status"] == "error" and result["request_id"] == "fixture"
    assert result["outcome"] == "timeout" and "command" not in result
