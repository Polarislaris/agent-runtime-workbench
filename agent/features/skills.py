"""Skill discovery and loading.

The system prompt receives only a lightweight skill catalog. Full SKILL.md
content enters the conversation only when the model calls load_skill.
"""

from __future__ import annotations

from ..config import SKILLS_DIR
from .frontmatter import parse_frontmatter


SKILL_REGISTRY: dict[str, dict] = {}


def scan_skills() -> None:
    """Scan agent/skills and keep each skill's catalog metadata and full text."""
    SKILL_REGISTRY.clear()
    if not SKILLS_DIR.exists():
        return

    for directory in sorted(SKILLS_DIR.iterdir()):
        if not directory.is_dir():
            continue

        manifest = directory / "SKILL.md"
        if not manifest.is_file():
            continue

        raw = manifest.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_frontmatter(raw)
        fallback_title = body.splitlines()[0].lstrip("#").strip() if body else directory.name
        name = str(meta.get("name") or directory.name).strip()
        description = str(meta.get("description") or fallback_title).strip()
        SKILL_REGISTRY[name] = {
            "name": name,
            "description": description,
            "content": raw,
        }


def list_skills() -> str:
    """Return the lightweight skill catalog for the system prompt."""
    if not SKILL_REGISTRY:
        return "- (no skills found)"
    return "\n".join(
        f"- {skill['name']}: {skill['description']}"
        for skill in SKILL_REGISTRY.values()
    )


def load_skill(name: str) -> str:
    """Tool handler: load one full skill document by exact skill name."""
    skill_name = str(name).strip()
    skill = SKILL_REGISTRY.get(skill_name)
    if not skill:
        available = ", ".join(SKILL_REGISTRY) or "(none)"
        return f"Skill not found: {skill_name}. Available skills: {available}"

    print(f"\033[96m[Skill loaded] Using skill: {skill_name}\033[0m")
    return skill["content"]

