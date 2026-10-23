"""A CLI nested under a Kanban worker must not inherit task ownership."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from hermes_cli import main as cli


class StopBeforeModel(Exception):
    pass


def _stop_at_safe_mode(monkeypatch, check):
    def stop(args):
        check()
        raise StopBeforeModel

    monkeypatch.setattr(cli, "_apply_safe_mode", stop)


def test_nested_cli_detaches_before_model(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_GOAL_MODE", "1")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    monkeypatch.setenv("TERMINAL_CWD", "/parent")
    monkeypatch.setattr("hermes_cli.nested_chat_identity.has_same_kanban_ancestor", lambda: True)

    def check():
        assert not any(k.startswith("HERMES_KANBAN_") for k in os.environ)
        assert os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") == "1"
        assert "TERMINAL_CWD" not in os.environ
        from tools import kanban_tools

        assert not kanban_tools._check_kanban_mode()
        with pytest.raises(Exception, match="not Kanban run owners"):
            kanban_tools._reject_delegated_child_mutation("kanban_complete")
        with pytest.raises(Exception, match="not Kanban run owners"):
            kanban_tools._reject_delegated_child_mutation("kanban_block")

    _stop_at_safe_mode(monkeypatch, check)
    with pytest.raises(StopBeforeModel):
        cli.cmd_chat(SimpleNamespace())


def test_dispatcher_worker_identity_is_unchanged(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_owner")
    monkeypatch.setattr("hermes_cli.nested_chat_identity.has_same_kanban_ancestor", lambda: False)

    def check():
        assert os.environ["HERMES_KANBAN_TASK"] == "t_owner"
        assert "HERMES_DELEGATED_CHILD_CONTEXT" not in os.environ

    _stop_at_safe_mode(monkeypatch, check)
    with pytest.raises(StopBeforeModel):
        cli.cmd_chat(SimpleNamespace())
