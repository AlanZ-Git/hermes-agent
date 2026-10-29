"""Tool-gated, on-demand discovery guidance for the public skills prompt."""


def build_skill_discovery_guidance(available_tools: set[str] | None) -> str:
    """Keep discovery available even when the automatic skills index is empty."""
    if available_tools is None or "skills_list" not in available_tools:
        return ""
    load_guidance = (
        "Then selectively load relevant skill text with skill_view(name); never bulk-load skill bodies. "
        if "skill_view" in available_tools else
        "Skill text loading is unavailable in this session; the catalog alone is not the full instructions. "
    )
    return (
        "### On-demand skill discovery\n"
        "Before choosing another approach for a concrete operation, deliverable, or way of working, "
        "check whether already loaded skills or startup descriptions explicitly cover it. Generic topical "
        "relevance does not mean the action is covered. If it is not explicitly covered, or descriptions "
        "are insufficient to judge, call skills_list() to list the current profile's discoverable skills "
        "first; the user need not know skill names. This applies even when the automatic index is empty. "
        "The catalog exposes names, descriptions, and categories, including hidden skills but excluding "
        "disabled skills, under the existing scope and platform rules. "
        + load_guidance
        + "Reuse a still-valid catalog already in context; refresh it after profile/scope or configuration "
        "changes, or when catalog context is lost or stale. Do not list for every chat message or for "
        "continuous steps already covered by known skills. A failed query is not evidence of absence; "
        "report the failure rather than claiming no skills exist. If there is no genuine match, proceed "
        "without forcing near-synonyms. Discovery or loading alone does not authorize sending or "
        "execution, including external consultation; execution remains subject to the current user's "
        "permission and the relevant skill. Never bypass disabled skills."
    )
