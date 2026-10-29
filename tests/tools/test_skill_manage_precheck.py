"""Profile-scoped pending precheck guidance (change two of #205).

Exercise the registered definition and dispatch, not the description helper.
"""
import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import skill_manager_tool as sm
from tools.registry import registry


WORK_PROFILES = ("default", "bridge", "collector", "dean", "dev", "publisher", "steward")
EXCLUDED_PROFILES = ("life", "everos-pilot", "unknown", "custom", "Dev", "developer")


@pytest.fixture
def profile_root(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    for profile in WORK_PROFILES + EXCLUDED_PROFILES:
        (root / "profiles" / profile).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    # Both launcher pins conflict with at least one served ContextVar identity.
    monkeypatch.setenv("HERMES_PROFILE_NAME", "life")
    monkeypatch.setenv("HERMES_PROFILE", "dev")
    return root


@contextmanager
def served(home):
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def definition():
    return registry.get_definitions({"skill_manage"}, quiet=True)[0]["function"]


def assert_precheck(description):
    """Pin the approved semantic clauses, without prescribing the entire wording."""
    for clause in (
        "relevant skill", "segments", "lawfully visible", "same-target pending",
        "this profile", "evidence-backed net changes", "reuse", "same-change record",
        "renam", "one coherent improvement", "related files", "unrelated",
        "reusable methods", "project facts", "one-off results", "applicability",
        "verifiable preference provenance", "local", "global", "no submission",
        "valuable unique",
    ):
        assert clause in description, clause
    # Patch preference is already in the original surface; don't add a tutorial.
    assert "targeted old_string/new_string fix — preferred" in description
    assert len(description.encode("utf-8")) < 1800


@pytest.mark.parametrize("profile", WORK_PROFILES + EXCLUDED_PROFILES)
def test_registered_description_by_exact_profile(profile_root, profile):
    before = copy.deepcopy(sm.SKILL_MANAGE_SCHEMA)
    home = profile_root if profile == "default" else profile_root / "profiles" / profile
    with served(home):
        actual = definition()
    if profile in WORK_PROFILES:
        assert_precheck(actual["description"])
        assert actual["description"].startswith(before["description"])
    else:
        assert actual == before
    assert actual["parameters"] == before["parameters"]
    assert sm.SKILL_MANAGE_SCHEMA == before


@pytest.mark.parametrize("suffix", ("dev", "profiles/dev", ".hermes", "profiles/bridge/nested"))
def test_custom_path_lookalike_is_not_a_work_profile(profile_root, suffix):
    with served(profile_root.parent / "unrelated" / suffix):
        assert definition() == sm.SKILL_MANAGE_SCHEMA


def test_unknown_resolution_does_not_fall_back_to_default(profile_root, monkeypatch):
    from hermes_cli import profiles

    def unavailable():
        raise OSError("synthetic unresolved home")

    monkeypatch.setattr(profiles, "get_active_profile_name", unavailable)
    with served(profile_root / "profiles/dev"):
        assert definition() == sm.SKILL_MANAGE_SCHEMA


def test_launcher_identity_is_resolved_on_each_registry_request(profile_root, monkeypatch):
    # Outside a ContextVar scope the trusted API honors launcher pins, not a basename.
    with served(None):
        for profile in ("dev", "life", "default", "unknown", "steward"):
            monkeypatch.setenv("HERMES_PROFILE_NAME", profile)
            result = definition()
            if profile in WORK_PROFILES:
                assert_precheck(result["description"])
            else:
                assert result == sm.SKILL_MANAGE_SCHEMA


def test_sequential_and_concurrent_profiles_do_not_leak(profile_root):
    before = copy.deepcopy(sm.SKILL_MANAGE_SCHEMA)
    environment = dict(os.environ)
    profiles = ("dev", "life", "bridge", "everos-pilot", "unknown", "default")

    def resolve(profile, barrier=None):
        home = profile_root if profile == "default" else profile_root / "profiles" / profile
        with served(home):
            if barrier:
                barrier.wait(timeout=10)
            results = [definition() for _ in range(3)]
        for result in results:
            if profile in WORK_PROFILES:
                assert_precheck(result["description"])
            else:
                assert result == before
            assert result["parameters"] == before["parameters"]
        return results[0]

    for profile in profiles + tuple(reversed(profiles)):
        resolve(profile)
    barrier = Barrier(len(profiles))
    with ThreadPoolExecutor(max_workers=len(profiles)) as pool:
        futures = [pool.submit(resolve, profile, barrier) for profile in profiles]
        for future in futures:
            future.result(timeout=20)
    assert dict(os.environ) == environment
    assert sm.SKILL_MANAGE_SCHEMA == before


@pytest.mark.parametrize("profile", ("dev", "life"))
@pytest.mark.parametrize("batch", (False, True))
def test_definition_rendering_keeps_native_stage_return(profile_root, monkeypatch, profile, batch):
    from tools import write_approval as wa
    from tools.skill_provenance import reset_current_write_origin, set_current_write_origin

    monkeypatch.setattr(wa, "write_approval_enabled", lambda subsystem: True)
    home = profile_root / "profiles" / profile
    op = {"name": "synthetic-method", "action": "patch", "old_string": "Old rule.",
          "new_string": "Use the verified rule only under its tested conditions."}
    payload = {"operations": [op]} if batch else op
    origin_token = set_current_write_origin("foreground")
    try:
        with served(home):
            definition()
            old = wa.stage_write(wa.SKILLS, {"action": "create", "name": "untouched"},
                                 summary="synthetic old pending", origin="foreground")
            old_path = home / "pending/skills" / (old["id"] + ".json")
            old_bytes = old_path.read_bytes()
            result = json.loads(registry.dispatch("skill_manage", payload))
            assert set(result) == {"success", "staged", "pending_id", "gist", "message"}
            assert result["success"] is True and result["staged"] is True
            assert result["message"] == wa.evaluate_gate(wa.SKILLS).message
            staged = wa.get_pending(wa.SKILLS, result["pending_id"])
            assert staged["origin"] == "foreground"
            if batch:
                assert staged["payload"]["operations"] == [op]
            else:
                assert staged["payload"] == {**op, "replace_all": False}
            assert result["gist"] == staged["summary"]
            assert old_path.read_bytes() == old_bytes
            assert not (home / "skills/synthetic-method").exists()
    finally:
        reset_current_write_origin(origin_token)
