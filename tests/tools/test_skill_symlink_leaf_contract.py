"""Installed directory-symlink leaves remain discoverable without following categories."""
from tools.skill_manager_tool import _iter_skill_dirs


def test_lexical_symlink_leaf_discovery(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("fixture")
    leaf = root / "linked"
    leaf.symlink_to(source, target_is_directory=True)
    (root / "broken").symlink_to(tmp_path / "absent")
    assert list(_iter_skill_dirs(root)) == [leaf]
