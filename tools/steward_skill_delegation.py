"""Host-installed scoped delegation; absent policy never grants authority.

The profile may install steward-delegation.json with the existing exact
identity/root fields, plus optional preservation_file/preservation_sha256.
These are private deployment inputs, never a model tool's arguments.
"""
import json
from pathlib import Path

from hermes_constants import get_hermes_home


def _host_policy():
    path = get_hermes_home() / "steward-delegation.json"
    try:
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def get_delegation():
    policy = _host_policy()
    if not policy:
        return None
    return {key: value for key, value in policy.items()
            if key not in {"preservation_file", "preservation_sha256", "closeout"}}


def get_closeout_policy():
    value = (_host_policy() or {}).get("closeout")
    return value if isinstance(value, dict) and value.get("enabled") is True else None


def get_preservation_binding():
    policy = _host_policy() or {}
    path, digest = policy.get("preservation_file"), policy.get("preservation_sha256")
    if not isinstance(path, str) or not isinstance(digest, str):
        return None
    import re
    if not Path(path).is_absolute() or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid preservation binding")
    return Path(path), digest
