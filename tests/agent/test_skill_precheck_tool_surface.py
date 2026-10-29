"""Real foreground/review construction sees the profile-scoped skill schema.

No model call: provider I/O is a tripwire. One-shot policy is deliberately not
cleared; in a finite harness we verify the pre-pruning definition and its removal.
"""
import copy
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.mark.parametrize("profile", ("dev", "life", "default"))
@pytest.mark.parametrize("routed", (False, True))
def test_foreground_and_review_definition_construction(tmp_path, monkeypatch, profile, routed):
    import model_tools
    import run_agent
    from agent import background_review
    from agent.oneshot_footprint import is_single_query_session
    from tools.skill_manager_tool import SKILL_MANAGE_SCHEMA

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    home = root if profile == "default" else root / "profiles" / profile
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_PROFILE_NAME", "life" if profile != "life" else "dev")
    monkeypatch.setenv("HERMES_PROFILE", "unknown")
    # Explicit synthetic runtime: no provider discovery or credential lookup is needed.
    monkeypatch.setattr(run_agent.AIAgent, "run_conversation", Mock(side_effect=AssertionError("no model calls")))
    real_definitions = model_tools.get_tool_definitions
    constructed = []

    def capture_definitions(*args, **kwargs):
        result = real_definitions(*args, **kwargs)
        constructed.append(copy.deepcopy(result))
        return result

    monkeypatch.setattr(model_tools, "get_tool_definitions", capture_definitions)
    parent = fork = None
    token = set_hermes_home_override(home)
    try:
        parent = run_agent.AIAgent(
            model="synthetic-model", provider="openai", api_key="synthetic-not-a-secret",
            base_url="http://127.0.0.1:1/v1", enabled_toolsets=["skills"],
            quiet_mode=True, skip_memory=True, skip_context_files=True,
            skip_background_review=True, max_iterations=1,
        )
        parent_tools = copy.deepcopy(parent.tools)
        foreground = next(t["function"] for t in constructed[-1]
                          if t["function"]["name"] == "skill_manage")
        if profile == "life":
            assert foreground == SKILL_MANAGE_SCHEMA
        else:
            assert "evidence-backed net changes" in foreground["description"]
        assert foreground["parameters"] == SKILL_MANAGE_SCHEMA["parameters"]
        if routed:
            # Stub only runtime resolution, not AIAgent construction or tool loading.
            runtime = {**parent._current_main_runtime(), "provider": "openai",
                       "model": "synthetic-review-model", "routed": True}
            monkeypatch.setattr(background_review, "_resolve_review_runtime", lambda *a: runtime)
        fork, runtime, was_routed = background_review.build_cache_parity_fork(
            parent, task_cfg={}, max_iterations=1)
        assert was_routed is routed
        reviewed = next(t["function"] for t in constructed[-1]
                        if t["function"]["name"] == "skill_manage")
        assert reviewed == foreground
        if is_single_query_session():
            assert "skill_manage" not in parent.valid_tool_names
            assert "skill_manage" not in fork.valid_tool_names
        else:
            assert next(t["function"] for t in parent.tools
                        if t["function"]["name"] == "skill_manage") == foreground
            assert next(t["function"] for t in fork.tools
                        if t["function"]["name"] == "skill_manage") == foreground
        if not routed:
            assert fork.tools == parent_tools
            assert fork.tools is not parent.tools
        assert parent.tools == parent_tools
        assert fork._persist_disabled is True
        assert fork._memory_write_origin == "background_review"
        assert len(constructed) >= 2
        run_agent.AIAgent.run_conversation.assert_not_called()

        # Traverse the worker's real fork/whitelist/run/cleanup path too. The
        # conversation entry is a recorder, not a fabricated model response.
        at_conversation = []

        def record_conversation(review, **kwargs):
            from hermes_cli.plugins import get_pre_tool_call_block_message

            at_conversation.append(copy.deepcopy(review.tools))
            assert review._memory_write_origin == "background_review"
            assert get_pre_tool_call_block_message("terminal", {}) is not None
            assert get_pre_tool_call_block_message("skill_manage", {}) is None
            return {}

        monkeypatch.setattr(run_agent.AIAgent, "run_conversation", record_conversation)
        state = background_review._ReviewForkState()
        background_review._run_review_fork(
            parent, [], background_review._SKILL_REVIEW_PROMPT, {}, None, state)
        assert len(at_conversation) == 1
        assert at_conversation[0] == fork.tools
        assert state.review_agent is None
    finally:
        if fork is not None:
            fork.close()
        if parent is not None:
            parent.close()
        reset_hermes_home_override(token)
