"""Provider-level and model-level context windows refresh a live compressor."""
from types import SimpleNamespace

import pytest

from agent.context_compressor import ContextCompressor
from tui_gateway import server


@pytest.mark.parametrize("model_level", [False, True])
def test_custom_window_refresh(monkeypatch, model_level):
    cc = ContextCompressor(model="fixture-model", threshold_percent=0.85, config_context_length=256000, quiet_mode=True)
    agent = SimpleNamespace(model="fixture-model", provider="custom", base_url="https://example.invalid/v1",
                            context_compressor=cc, compression_enabled=True)
    entry = {"name": "fixture", "base_url": agent.base_url}
    slot = {"context_length": 256000}
    if model_level:
        entry["models"] = {agent.model: slot}
    else:
        entry.update(slot)
        slot = entry
    cfg = {"model": {"default": agent.model, "provider": "custom:fixture"},
           "custom_providers": [entry], "compression": {"threshold": 0.85}}
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    session = {"agent": agent}
    server._sync_agent_compression_with_config("fixture", session)
    slot["context_length"] = 350000
    server._sync_agent_compression_with_config("fixture", session)
    assert cc.context_length == 350000
