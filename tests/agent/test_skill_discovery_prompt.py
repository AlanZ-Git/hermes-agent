"""On-demand discovery through the public skills prompt (TODO201)."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import prompt_builder, skill_utils, system_prompt
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import skills_tool


@pytest.fixture
def skill_home(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PLATFORM", "cli")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    # Exercise local catalog/config resolution without activating optional plugins.
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    prompt_builder.clear_skills_system_prompt_cache()
    skills_tool.clear_skills_cache()
    skill_utils._raw_config_cache_clear()
    yield home
    prompt_builder.clear_skills_system_prompt_cache()
    skills_tool.clear_skills_cache()
    skill_utils._raw_config_cache_clear()


def test_empty_index_still_teaches_discovery_with_tools(skill_home):
    (skill_home / "skills").mkdir()

    prompt = prompt_builder.build_skills_system_prompt(
        available_tools={"skills_list", "skill_view"},
    )

    assert "skills_list()" in prompt
    assert "skill_view" in prompt
    assert "<available_skills>" not in prompt


def _skill(home, name, *, description=None, marker=None, frontmatter=""):
    directory = home / "skills" / "operations" / name
    directory.mkdir(parents=True)
    marker = marker or f"EXACT_BODY_{name.upper().replace('-', '_')}"
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description or name + ' instructions'}\n"
        f"{frontmatter}---\n{marker}\n",
        encoding="utf-8",
    )
    return marker


def _config(home, **skills):
    (home / "config.yaml").write_text(json.dumps({"skills": skills}), encoding="utf-8")


def _assert_discovery_contract(prompt):
    # Inspect the generated public prompt, not a constant or source text. Model
    # compliance with this prose is validated separately by the owner.
    for instruction in (
        "concrete operation, deliverable, or way of working",
        "already loaded skills or startup descriptions explicitly cover it",
        "Generic topical relevance does not mean the action is covered",
        "descriptions are insufficient to judge",
        "skills_list()",
        "current profile's discoverable skills",
        "user need not know skill names",
        "names, descriptions, and categories",
        "including hidden skills but excluding disabled skills",
        "selectively load relevant skill text with skill_view(name)",
        "never bulk-load skill bodies",
        "Reuse a still-valid catalog already in context",
        "profile/scope or configuration changes",
        "catalog context is lost or stale",
        "Do not list for every chat message",
        "continuous steps already covered",
        "A failed query is not evidence of absence",
        "without forcing near-synonyms",
        "Discovery or loading alone does not authorize sending or execution",
        "execution remains subject to the current user's permission and the relevant skill",
        "Never bypass disabled skills",
    ):
        assert instruction in prompt


@pytest.mark.parametrize("single_query", [False, True])
@pytest.mark.parametrize("layout", ["missing", "empty", "only-hidden"])
def test_discovery_survives_empty_automatic_index(skill_home, monkeypatch, single_query, layout):
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1" if single_query else "0")
    marker = None
    if layout == "empty":
        (skill_home / "skills").mkdir()
    elif layout == "only-hidden":
        marker = _skill(skill_home, "release-check")
        _config(skill_home, hidden=["release-check"])

    prompt = prompt_builder.build_skills_system_prompt(available_tools={"skills_list", "skill_view"})
    _assert_discovery_contract(prompt)
    assert "<available_skills>" not in prompt
    assert "release-check" not in prompt
    assert "skill_manage" not in prompt

    listing = json.loads(skills_tool.skills_list())
    assert listing["success"]
    if marker:
        assert listing["skills"] == [{
            "name": "release-check", "description": "release-check instructions", "category": "operations",
        }]
        assert marker not in json.dumps(listing)
        viewed = json.loads(skills_tool.skill_view("release-check", preprocess=False))
        assert viewed["success"] and marker in viewed["content"]
    else:
        assert listing["skills"] == []


@pytest.mark.parametrize("single_query", [False, True])
@pytest.mark.parametrize("platform_policy", [False, True])
def test_metadata_discovery_then_selective_view_respects_visibility(
    skill_home, monkeypatch, single_query, platform_policy,
):
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1" if single_query else "0")
    names = ("resident", "hidden", "disabled", "both")
    bodies = {name: _skill(skill_home, name) for name in names}
    if platform_policy:
        _config(skill_home, platform_hidden={"cli": ["hidden", "both"]},
                platform_disabled={"cli": ["disabled", "both"]})
    else:
        _config(skill_home, hidden=["hidden", "both"], disabled=["disabled", "both"])
    prompt = prompt_builder.build_skills_system_prompt(available_tools={"skills_list", "skill_view"})
    _assert_discovery_contract(prompt)
    assert "- resident: resident instructions" in prompt
    for name in names[1:]:
        assert f"- {name}:" not in prompt

    catalog = skills_tool.skills_list()
    listing = json.loads(catalog)
    assert listing["success"]
    assert listing["categories"] == ["operations"]
    assert listing["skills"] == [
        {"name": name, "description": f"{name} instructions", "category": "operations"}
        for name in ("hidden", "resident")
    ]
    assert not any(body in prompt or body in catalog for body in bodies.values())
    viewed = json.loads(skills_tool.skill_view("hidden", preprocess=False))
    assert viewed["success"] and bodies["hidden"] in viewed["content"]
    assert all(body not in viewed["content"] for name, body in bodies.items() if name != "hidden")
    for name in ("disabled", "both"):
        refused = json.loads(skills_tool.skill_view(name, preprocess=False))
        assert not refused["success"]
        assert bodies[name] not in json.dumps(refused)


@pytest.mark.parametrize("single_query", [False, True])
@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize("tools", [set(), {"skill_view"}, {"skills_list"}, {"skills_list", "skill_view"}])
def test_public_prompt_only_names_available_read_tools(skill_home, monkeypatch, single_query, populated, tools):
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1" if single_query else "0")
    if populated:
        _skill(skill_home, "resident")
    prompt = prompt_builder.build_skills_system_prompt(available_tools=tools)
    assert ("skills_list()" in prompt) == ("skills_list" in tools)
    if "skill_view" not in tools:
        assert "skill_view" not in prompt
    if {"skills_list", "skill_view"} <= tools:
        _assert_discovery_contract(prompt)


def test_unknown_and_empty_tool_surfaces_do_not_share_cached_instructions(skill_home):
    _skill(skill_home, "resident")
    legacy = prompt_builder.build_skills_system_prompt()
    assert "skill_view" in legacy
    assert "skills_list()" not in legacy
    no_tools = prompt_builder.build_skills_system_prompt(available_tools=set())
    assert "resident instructions" in no_tools
    assert "skill_view" not in no_tools and "skills_list()" not in no_tools
    assert prompt_builder.build_skills_system_prompt() == legacy


def test_warm_catalog_reuse_and_config_refresh(skill_home, monkeypatch):
    _skill(skill_home, "resident")
    _skill(skill_home, "hidden")
    _config(skill_home, hidden=["hidden"])
    tools = {"skills_list", "skill_view"}
    prompt = prompt_builder.build_skills_system_prompt(available_tools=tools)
    catalog = skills_tool.skills_list()
    parse = Mock(wraps=prompt_builder._parse_skill_file)
    read = Mock(wraps=skills_tool._read_skill_text)
    monkeypatch.setattr(prompt_builder, "_parse_skill_file", parse)
    monkeypatch.setattr(skills_tool, "_read_skill_text", read)
    assert prompt_builder.build_skills_system_prompt(available_tools=tools) == prompt
    assert skills_tool.skills_list() == catalog
    parse.assert_not_called()
    read.assert_not_called()

    # No explicit cache clear: both public surfaces must honor config changes.
    _config(skill_home, hidden=["hidden"], disabled=["resident", "hidden"])
    refreshed = prompt_builder.build_skills_system_prompt(available_tools=tools)
    _assert_discovery_contract(refreshed)
    assert "<available_skills>" not in refreshed
    assert json.loads(skills_tool.skills_list())["skills"] == []


def _agent(home, tools):
    return SimpleNamespace(
        load_soul_identity=False, skip_context_files=True, valid_tool_names=tools,
        _task_completion_guidance=False, _parallel_tool_call_guidance=False,
        _tool_use_enforcement=False, _execution_guidance=False,
        _environment_probe=False, _bot_mode_protocol=False, _kanban_worker_guidance="",
        _memory_store=None, _memory_manager=None, _plugin_system_prompt_sections_snapshot=(),
        _session_db=SimpleNamespace(db_path=home / "state.db"),
        model="", provider="", platform="cli", pass_session_id=False, session_id="",
        _emit_diagnostic_status=lambda *_args, **_kwargs: None,
    )


@pytest.mark.parametrize("single_query", [False, True])
@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize("tools", [set(), {"terminal"}, {"skill_view"}, {"skills_list"}, {"skills_list", "skill_view"}])
def test_full_system_prompt_wires_index_and_tool_gates(skill_home, monkeypatch, single_query, populated, tools):
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1" if single_query else "0")
    bodies = []
    if populated:
        bodies = [_skill(skill_home, name) for name in ("resident", "hidden", "disabled", "both")]
        _config(skill_home, hidden=["hidden", "both"], disabled=["disabled", "both"])
    monkeypatch.setattr(prompt_builder, "build_environment_hints", lambda: "")
    agent = _agent(skill_home, tools)
    full = system_prompt.build_system_prompt(agent, system_message="EXACT_CALLER_CONTEXT")
    assert not any(body in full for body in bodies)
    assert ("resident instructions" in full) == (populated and bool(tools & {"skills_list", "skill_view"}))
    for name in ("hidden", "disabled", "both"):
        assert f"- {name}:" not in full
    assert "EXACT_CALLER_CONTEXT" in full
    assert "Conversation started:" in full
    assert ("skills_list()" in full) == ("skills_list" in tools)
    if "skill_view" not in tools:
        assert "skill_view" not in full
    if {"skills_list", "skill_view"} <= tools:
        _assert_discovery_contract(full)
        assert full.index("EXACT_CALLER_CONTEXT") < full.index("### On-demand skill discovery")
        assert "### On-demand skill discovery" not in agent._cached_system_prompt_static
    assert system_prompt.build_system_prompt(agent, system_message="EXACT_CALLER_CONTEXT") == full


def test_profile_scopes_do_not_leak_prompt_catalog_or_bodies(skill_home, tmp_path, monkeypatch):
    other = tmp_path / "other-profile"
    first_body = _skill(skill_home, "shared-name", description="First profile instructions", marker="EXACT_FIRST_BODY")
    other_body = _skill(other, "shared-name", description="Other profile instructions", marker="EXACT_OTHER_BODY")
    _config(other, hidden=["shared-name"])
    tools = {"skills_list", "skill_view"}
    first = prompt_builder.build_skills_system_prompt(available_tools=tools)
    first_catalog = skills_tool.skills_list()
    assert "First profile instructions" in first
    assert "First profile instructions" in first_catalog

    # The session's own home wins even when the ambient environment belongs to another profile.
    monkeypatch.setattr(prompt_builder, "build_environment_hints", lambda: "")
    agent = _agent(other, tools)
    full = system_prompt.build_system_prompt(agent)
    _assert_discovery_contract(full)
    assert "First profile instructions" not in full
    assert "Other profile instructions" not in full
    assert first_body not in full and other_body not in full

    token = set_hermes_home_override(str(other))
    try:
        listing = json.loads(skills_tool.skills_list())
        assert listing["skills"] == [{
            "name": "shared-name", "description": "Other profile instructions", "category": "operations",
        }]
        viewed = json.loads(skills_tool.skill_view("shared-name", preprocess=False))
        assert viewed["success"] and other_body in viewed["content"]
        assert first_body not in viewed["content"]
    finally:
        reset_hermes_home_override(token)
    assert prompt_builder.build_skills_system_prompt(available_tools=tools) == first
    assert skills_tool.skills_list() == first_catalog
    assert first_body in json.loads(skills_tool.skill_view("shared-name", preprocess=False))["content"]


def test_names_only_descriptions_can_be_discovered_without_loading_bodies(skill_home):
    marker = _skill(skill_home, "release-check", description="Verify a signed release archive")
    prompt = prompt_builder.build_skills_system_prompt(
        available_tools={"skills_list", "skill_view"}, compact_categories=frozenset({"operations"}),
    )
    assert "operations [names only]: release-check" in prompt
    assert "Verify a signed release archive" not in prompt
    _assert_discovery_contract(prompt)
    catalog = skills_tool.skills_list()
    assert "Verify a signed release archive" in catalog
    assert marker not in prompt and marker not in catalog


@pytest.mark.platforms("linux")
def test_discovery_preserves_host_platform_offer_rules(skill_home):
    marker = _skill(skill_home, "windows-only", frontmatter="platforms: [windows]\n")
    prompt = prompt_builder.build_skills_system_prompt(available_tools={"skills_list", "skill_view"})
    _assert_discovery_contract(prompt)
    assert "windows-only" not in prompt
    assert json.loads(skills_tool.skills_list())["skills"] == []
    # Existing host-platform restrictions also reject explicit reads.
    viewed = json.loads(skills_tool.skill_view("windows-only", preprocess=False))
    assert not viewed["success"]
    assert viewed["readiness_status"] == "unsupported"
    assert marker not in json.dumps(viewed)


def test_suppressed_index_keeps_discovery_without_exposing_rows(skill_home):
    _skill(skill_home, "resident")
    _config(skill_home, hidden=42)
    prompt = prompt_builder.build_skills_system_prompt(available_tools={"skills_list", "skill_view"})
    _assert_discovery_contract(prompt)
    assert "resident instructions" not in prompt
    assert "<available_skills>" not in prompt
    legacy_prompt = prompt_builder.build_skills_system_prompt()
    assert "Skill discovery is incomplete" in legacy_prompt
    assert "do not assume the directory is empty" in legacy_prompt
    assert "resident instructions" not in legacy_prompt
    assert "<available_skills>" not in legacy_prompt


def test_safe_worker_keeps_empty_prompt_even_with_discovery_tools(skill_home, monkeypatch):
    _skill(skill_home, "resident")
    monkeypatch.setattr("agent.safe_worker_policy.safe_worker_enabled", lambda: True)
    tools = {"skills_list", "skill_view"}
    assert prompt_builder.build_skills_system_prompt(available_tools=tools) == ""
    monkeypatch.setattr(prompt_builder, "build_environment_hints", lambda: "")
    full = system_prompt.build_system_prompt(_agent(skill_home, tools))
    assert "### On-demand skill discovery" not in full
    assert "resident instructions" not in full


def test_failed_listing_is_not_a_successful_empty_catalog(skill_home, monkeypatch):
    def unavailable():
        raise OSError("synthetic catalog unavailable")

    monkeypatch.setattr(skills_tool, "find_all_skills", unavailable)
    result = json.loads(skills_tool.skills_list())
    assert not result["success"]
    assert "synthetic catalog unavailable" in result["error"]
    prompt = prompt_builder.build_skills_system_prompt(available_tools={"skills_list", "skill_view"})
    _assert_discovery_contract(prompt)
