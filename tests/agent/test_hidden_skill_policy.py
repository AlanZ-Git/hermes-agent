"""Hidden is offer-time visibility, never permission to bypass disabled skills."""
import json

import pytest

from agent import prompt_builder, skill_utils


@pytest.mark.parametrize("config", [
    "skills:\n  hidden: [private-skill]\n  disabled: [blocked-skill]\n",
    "skills:\n  platform_hidden: {cli: [private-skill]}\n  disabled: [blocked-skill]\n",
])
def test_hidden_index_snapshot_and_explicit_load(tmp_path, monkeypatch, config):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PLATFORM", "cli")
    for name in ("resident-skill", "private-skill", "blocked-skill"):
        directory = tmp_path / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: fixture\n---\n{name}\n"
        )
    prompt_builder.clear_skills_system_prompt_cache(clear_snapshot=True)
    assert "private-skill" in prompt_builder.build_skills_system_prompt()
    (tmp_path / "config.yaml").write_text(config)
    skill_utils._raw_config_cache_clear()
    index = prompt_builder.build_skills_system_prompt()
    assert "resident-skill" in index
    assert "private-skill" not in index
    assert "blocked-skill" not in index
    from tools.skills_tool import skill_view
    from agent.skill_commands import build_preloaded_skills_prompt
    assert json.loads(skill_view("private-skill"))["success"]
    assert not json.loads(skill_view("blocked-skill"))["success"]
    _, loaded, missing = build_preloaded_skills_prompt(["private-skill", "blocked-skill"])
    assert loaded == ["private-skill"]
    assert missing == ["blocked-skill"]


@pytest.mark.parametrize("config", [
    "skills: [\n", "[not-a-mapping]\n", "skills:\n  hidden: 42\n",
    "skills:\n  hidden: [42, alpha]\n", "skills:\n  platform_hidden: [cli]\n",
])
def test_invalid_hidden_policy_suppresses_index(tmp_path, monkeypatch, config):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    directory = tmp_path / "skills" / "resident-skill"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: resident-skill\ndescription: fixture\n---\nfixture\n"
    )
    (tmp_path / "config.yaml").write_text(config)
    skill_utils._raw_config_cache_clear()
    prompt_builder.clear_skills_system_prompt_cache(clear_snapshot=True)
    assert prompt_builder.build_skills_system_prompt() == ""
