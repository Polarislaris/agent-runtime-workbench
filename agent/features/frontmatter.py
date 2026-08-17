"""Tiny YAML-frontmatter parser used by skills and memory files."""

from __future__ import annotations


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Parse simple key/value frontmatter without pulling in a YAML dependency."""
    if not raw.startswith("---\n"):
        return {}, raw

    end = raw.find("\n---", 4)
    if end == -1:
        return {}, raw

    frontmatter = raw[4:end].strip("\n")
    body = raw[end + len("\n---"):].lstrip("\n")
    meta = {}
    lines = frontmatter.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]
        if not line.strip() or ":" not in line:
            index += 1
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()

        if value == "|":
            index += 1
            block_lines = []
            while index < len(lines) and (
                lines[index].startswith(" ") or not lines[index].strip()
            ):
                block_lines.append(lines[index].strip())
                index += 1
            meta[key] = " ".join(part for part in block_lines if part).strip()
            continue

        meta[key] = value.strip("\"'")
        index += 1

    return meta, body

