"""Shared bundles are read-only overlays derived from the active profile home."""
import pytest

from agent import skill_bundles as bundles
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_shared_bundle_overlay_and_scope_switch(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_BUNDLES_DIR", raising=False)
    shared = tmp_path / "shared-skill-bundles"
    shared.mkdir()
    (shared / "common.yaml").write_text("name: common\nskills: [shared-skill]\n")
    for name in ("one", "two"):
        home = tmp_path / "profiles" / name
        home.joinpath("skill-bundles").mkdir(parents=True)
    first = tmp_path / "profiles/one/skill-bundles/common.yaml"
    first.write_text("name: common\nskills: [local-skill]\n")
    token = set_hermes_home_override(str(tmp_path / "profiles/one"))
    try:
        assert bundles.get_skill_bundles()["/common"]["skills"] == ["local-skill"]
    finally:
        reset_hermes_home_override(token)
    token = set_hermes_home_override(str(tmp_path / "profiles/two"))
    try:
        assert bundles.get_skill_bundles()["/common"]["skills"] == ["shared-skill"]
        bundles.save_bundle("common", ["replacement"], overwrite=True)
        assert bundles.get_skill_bundles()["/common"]["skills"] == ["replacement"]
        assert (shared / "common.yaml").read_text() == "name: common\nskills: [shared-skill]\n"
    finally:
        reset_hermes_home_override(token)


def test_shared_bundle_symlink_write_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_BUNDLES_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    shared = tmp_path / "shared-skill-bundles"
    shared.mkdir()
    source = shared / "common.yaml"
    source.write_text("name: common\nskills: [shared-skill]\n")
    local = tmp_path / "skill-bundles"
    local.mkdir()
    (local / "common.yaml").symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        bundles.save_bundle("common", ["replacement"], overwrite=True)
    assert "shared-skill" in source.read_text()
