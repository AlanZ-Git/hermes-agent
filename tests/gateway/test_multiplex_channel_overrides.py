"""Channel routes belong to the active Profile, not the shared runner's startup snapshot."""

from pathlib import Path
import json

import pytest
import hermes_yaml as yaml

import gateway.run as gateway_run
from gateway.config import Platform, load_gateway_config
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionSource


def _write_profile(home, name, *, channel_model=None, channel_provider="primary", layout="platforms"):
    home.mkdir(parents=True, exist_ok=True)
    config = {
        "gateway": {"multiplex_profiles": name == "default"},
        "model": {"default": f"{name}-default", "provider": "custom:primary"},
        "custom_providers": [
            {"name": provider, "base_url": f"https://{name}-{provider}.invalid/v1",
             "api_key": "synthetic-test-key", "model": f"{name}-bundled"}
            for provider in ("primary", "alternate")
        ],
        "agent": {"system_prompt": f"{name}-global-prompt"},
        "platforms": {"telegram": {"channel_overrides": {}}},
    }
    if channel_model:
        config["platforms"]["telegram"]["channel_overrides"]["shared-chat"] = {
            "model": channel_model, "provider": f"custom:{channel_provider}",
            "system_prompt": f"{name}-channel-prompt",
        }
    platform = config["platforms"]["telegram"]
    if layout != "platforms":
        config.pop("platforms")
        if layout == "gateway_platforms":
            config["gateway"]["platforms"] = {"telegram": platform}
        elif layout == "gateway_platform":
            config["gateway"]["telegram"] = platform
        elif layout == "root_platform":
            config["telegram"] = platform
        else:
            (home / "gateway.json").write_text(json.dumps({"platforms": {"telegram": platform}}))
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return config


@pytest.mark.parametrize("layout", ["platforms", "gateway_platforms", "gateway_platform", "root_platform", "gateway_json"])
def test_shared_runner_resolves_each_profile_route_and_session_precedence(tmp_path, monkeypatch, layout):
    """The real scoped config/provider/runner chain isolates an identical chat id."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    homes = {name: home if name == "default" else home / "profiles" / name
             for name in ("default", "collector", "life")}
    configs = {
        "default": _write_profile(homes["default"], "default", layout=layout),
        "collector": _write_profile(homes["collector"], "collector", channel_model="collector-chat", layout=layout),
        "life": _write_profile(homes["life"], "life", channel_model="life-chat", channel_provider="alternate", layout=layout),
    }
    runner = object.__new__(GatewayRunner)
    with _profile_runtime_scope(home, prepared_secret_scope={}):
        runner.config = load_gateway_config()
    for name in ("collector", "life", "default", "life", "collector"):
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="shared-chat", profile=name)
        key = f"test-{name}"
        provider = "alternate" if name == "life" else "primary"
        expected_model = f"{name}-chat" if name != "default" else "default-default"
        with _profile_runtime_scope(homes[name], prepared_secret_scope={}):
            model, runtime = runner._resolve_session_agent_runtime(source=source, session_key=key)
            assert model == expected_model
            assert runtime["provider"] == "custom"
            assert runtime["requested_provider"] == f"custom:{provider}"
            assert runtime["base_url"] == f"https://{name}-{provider}.invalid/v1"
            assert runner._resolve_model_for_channel(Platform.TELEGRAM, "shared-chat") == expected_model
            expected_prompt = f"{name}-channel-prompt" if name != "default" else "default-global-prompt"
            assert runner._get_system_prompt_for_channel(Platform.TELEGRAM, "shared-chat") == expected_prompt
            other_model, other_runtime = runner._resolve_session_agent_runtime(
                source=SessionSource(platform=Platform.TELEGRAM, chat_id="other-chat", profile=name),
                session_key=f"other-{name}",
            )
            assert other_model == configs[name]["model"]["default"]
            assert other_runtime["base_url"] == f"https://{name}-primary.invalid/v1"
            # A model-only session override follows the normal (non-fast) route.
            runner._session_state(key).conversation.model_override = {"model": "session-model"}
            assert runner._resolve_session_agent_runtime(source=source, session_key=key)[0] == "session-model"
            # Fully resolved overrides take the fast path, ahead of channel/default providers.
            runner._session_state(key).conversation.model_override = {
                "model": "session-model", "provider": "custom", "requested_provider": "custom:session",
                "api_key": "synthetic-session-key", "base_url": "https://session.invalid/v1",
                "credential_pool": object(),
            }
            model, runtime = runner._resolve_session_agent_runtime(source=source, session_key=key)
            assert (model, runtime["requested_provider"], runtime["base_url"]) == (
                "session-model", "custom:session", "https://session.invalid/v1")
            runner._session_state(key).conversation.model_override = None


def test_channel_prompt_and_next_turn_edits_stay_profile_local(tmp_path, monkeypatch):
    """The turn prompt consumer and runtime resolver share profile-local, fresh overrides."""
    from concurrent.futures import ThreadPoolExecutor
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    routed = home / "profiles" / "collector"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    _write_profile(home, "default", channel_model="default-chat")
    config = _write_profile(routed, "collector", channel_model="collector-chat")
    runner = object.__new__(GatewayRunner)
    with _profile_runtime_scope(home, prepared_secret_scope={}):
        runner.config = load_gateway_config()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="shared-chat", profile="collector")
    turn = object.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = TurnContext(source=source, context_prompt="platform-context", channel_prompt="legacy-hint")

    def probe(profile_home, name):
        with _profile_runtime_scope(profile_home, prepared_secret_scope={}):
            prompt = turn._combined_ephemeral_prompt()
            assert prompt == f"platform-context\n\nlegacy-hint\n\n{name}-channel-prompt"
            assert runner._get_system_prompt_for_channel(Platform.TELEGRAM, "other-chat") == f"{name}-global-prompt"
            return runner._resolve_model_for_channel(Platform.TELEGRAM, "shared-chat")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(probe, profile_home, name)
                   for profile_home, name in [(home, "default"), (routed, "collector")] * 4]
        assert [f.result() for f in futures] == ["default-chat", "collector-chat"] * 4

    # No runner rebuild or config-cache reset: an on-disk edit reaches the next resolution.
    channel = config["platforms"]["telegram"]["channel_overrides"]["shared-chat"]
    channel.update(model="edited-chat-model", provider="custom:alternate", system_prompt="edited-prompt")
    (routed / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    with _profile_runtime_scope(routed, prepared_secret_scope={}):
        model, runtime = runner._resolve_session_agent_runtime(source=source, session_key="edited")
        assert (model, runtime["requested_provider"], runtime["base_url"]) == (
            "edited-chat-model", "custom:alternate", "https://collector-alternate.invalid/v1")
        assert turn._combined_ephemeral_prompt().endswith("\n\nedited-prompt")
        # Existing chat > thread > parent order also applies to scoped configs.
        overrides = config["platforms"]["telegram"]["channel_overrides"]
        overrides["parent"] = {"model": "parent-model"}
        overrides["thread"] = {"model": "thread-model"}
        (routed / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        assert runner._resolve_model_for_channel(Platform.TELEGRAM, "shared-chat", thread_id="thread", parent_id="parent") == "edited-chat-model"
        assert runner._resolve_model_for_channel(Platform.TELEGRAM, "missing", thread_id="thread", parent_id="parent") == "thread-model"
        assert runner._resolve_model_for_channel(Platform.TELEGRAM, "missing", parent_id="parent") == "parent-model"
        overrides.pop("shared-chat")
        (routed / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        assert runner._resolve_session_agent_runtime(source=source, session_key="edited")[0] == "collector-default"
        assert runner._get_system_prompt_for_channel(Platform.TELEGRAM, "shared-chat") == "collector-global-prompt"
    assert probe(home, "default") == "default-chat"
