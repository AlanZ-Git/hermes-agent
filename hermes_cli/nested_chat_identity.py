"""Separate nested interactive CLI sessions from their parent's run authority."""
import os

import psutil


def has_same_kanban_ancestor():
    task = os.environ.get("HERMES_KANBAN_TASK")
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not task:
        return False
    try:
        for process in psutil.Process().parents():
            env = process.environ()
            if env.get("HERMES_KANBAN_TASK") == task and (
                not run_id or env.get("HERMES_KANBAN_RUN_ID") == run_id
            ):
                return True
    except (psutil.Error, OSError):
        return False
    return False


def detach_nested_chat():
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            os.environ.pop(key, None)
    os.environ["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    if os.environ.get("HERMES_SESSION_SOURCE") == "kanban":
        os.environ.pop("HERMES_SESSION_SOURCE", None)
    os.environ.pop("TERMINAL_CWD", None)


def run_chat(args, launch):
    if not has_same_kanban_ancestor():
        return launch(args)
    detach_nested_chat()
    from agent.delegation_context import delegated_child_context
    with delegated_child_context():
        return launch(args)
