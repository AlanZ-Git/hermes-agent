"""Multiplex channel configuration follows the bound profile, including edits."""
from types import SimpleNamespace

from gateway.config import GatewayConfig, Platform
from gateway.run_config_loaders import GatewayConfigLoadersMixin
from gateway import run


def test_channel_override_uses_current_profile_not_launch_snapshot(monkeypatch):
    launch = GatewayConfig.from_dict({"platforms": {"telegram": {"channel_overrides": {"chat": {"model": "launch"}}}}})
    launch.multiplex_profiles = True
    runner = GatewayConfigLoadersMixin()
    runner.config = launch
    cfg = {"platforms": {"telegram": {"channel_overrides": {"chat": {"model": "scoped"}}}}}
    monkeypatch.setattr(run, "_load_gateway_config", lambda: cfg)
    monkeypatch.setattr("gateway.config_loader.load_legacy_gateway_json", lambda home: {})
    assert runner._channel_override(Platform.TELEGRAM, "chat", None, None).model == "scoped"
    cfg["platforms"]["telegram"]["channel_overrides"]["chat"]["model"] = "edited"
    assert runner._channel_override(Platform.TELEGRAM, "chat", None, None).model == "edited"
    cfg["platforms"]["telegram"]["channel_overrides"].clear()
    assert runner._channel_override(Platform.TELEGRAM, "chat", None, None) is None
