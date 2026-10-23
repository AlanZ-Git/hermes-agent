"""Delegated children cannot mutate a Kanban board through Studio MCP."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.delegation_context import delegated_child_context
from tools import mcp_tool_handlers as handlers


@pytest.mark.parametrize(
    "tool_name,args,denied",
    [
        ("hermes_studio_api_request", {"method": "POST", "path": "/api/hermes/kanban/complete"}, True),
        ("hermes_studio_api_request", {"method": "GET", "path": "/api/hermes/kanban/tasks"}, True),
        ("hermes_studio_api_request", {"method": "POST", "path": "/api/hermes/%6banban/complete"}, True),
        ("hermes_studio_api_request", {"method": "POST", "path": "/api/hermes/x/../kanban/complete"}, True),
        ("hermes_studio_api_request", {"method": "POST", "path": "  /api/hermes/kanban/complete  "}, True),
        ("hermes_studio_api_request", {"method": "POST", "path": "/api/hermes\\kanban/complete"}, True),
        ("hermes_studio_api_request", {"method": "GET", "path": "/api/studio/usage"}, False),
        ("hermes_studio_api_openapi_get", {}, False),
    ],
)
def test_child_board_ingress_only(monkeypatch, tool_name, args, denied):
    contacted = []
    server = SimpleNamespace(session=object())
    monkeypatch.setattr(handlers, "_trust_gate_check", lambda *a: None)
    monkeypatch.setattr(handlers, "_check_circuit_breaker", lambda *a: None)
    monkeypatch.setattr(
        handlers,
        "_acquire_call_server",
        lambda *a: (contacted.append(a) or server, None),
    )
    monkeypatch.setattr(handlers, "_dispatch", lambda *a, **k: "contacted")

    with delegated_child_context():
        result = handlers._make_tool_handler("any-server-alias", tool_name, 1)(args)

    assert bool(contacted) is not denied
    if denied:
        assert "delegated" in result.lower()


def test_parent_control_plane_not_denied(monkeypatch):
    contacted = []
    server = SimpleNamespace(session=object())
    monkeypatch.setattr(handlers, "_trust_gate_check", lambda *a: None)
    monkeypatch.setattr(handlers, "_check_circuit_breaker", lambda *a: None)
    monkeypatch.setattr(
        handlers,
        "_acquire_call_server",
        lambda *a: (contacted.append(a) or server, None),
    )
    monkeypatch.setattr(handlers, "_dispatch", lambda *a, **k: "contacted")

    handlers._make_tool_handler("hermes-studio-api", "hermes_studio_api_request", 1)(
        {"method": "POST", "path": "/api/hermes/kanban/complete"}
    )
    assert contacted
