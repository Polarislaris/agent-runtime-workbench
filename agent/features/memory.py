"""Persistent memory subsystem.

Memory stores durable knowledge in agent/.memory as Markdown files with
frontmatter. MEMORY.md is a cheap index for the system prompt; full files are
selected and injected only when relevant to the current turn.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..runtime.client import get_client
from ..config import (
    CONSOLIDATE_THRESHOLD,
    MAX_LOADED_MEMORIES,
    MEMORY_DIR,
    MEMORY_INDEX,
    MEMORY_TYPES,
    MODEL,
)
from .frontmatter import parse_frontmatter
from ..runtime.messages import extract_text, format_recent_messages


def _one_line(value: str, fallback: str = "") -> str:
    """Collapse metadata into one line so simple frontmatter stays valid."""
    text = str(value or fallback).strip()
    return re.sub(r"\s+", " ", text)


def _slugify_memory_name(name: str) -> str:
    """Turn a memory name into a readable, path-safe Markdown filename."""
    slug = _one_line(name, "memory").lower()
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", slug).strip("-")
    return slug or "memory"


def _memory_file_path(filename: str) -> Path:
    """Resolve an individual memory file without allowing path traversal."""
    safe_name = Path(str(filename)).name
    if safe_name == "MEMORY.md" or not safe_name.endswith(".md"):
        raise ValueError(f"Not an individual memory file: {filename}")

    path = (MEMORY_DIR / safe_name).resolve()
    if not path.is_relative_to(MEMORY_DIR.resolve()):
        raise ValueError(f"Memory path escapes .memory: {filename}")
    return path


def read_memory_index() -> str:
    """Read MEMORY.md for the system prompt."""
    if not MEMORY_INDEX.is_file():
        return "- (no persistent memories saved yet)"

    content = MEMORY_INDEX.read_text(encoding="utf-8", errors="replace").strip()
    return content or "- (memory index is empty)"


def list_memory_files() -> list[dict]:
    """Return metadata for every individual .memory/*.md memory file."""
    if not MEMORY_DIR.exists():
        return []

    memories = []
    for path in sorted(MEMORY_DIR.glob("*.md")):
        if path.name == "MEMORY.md" or not path.is_file():
            continue

        raw = path.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_frontmatter(raw)
        name = _one_line(meta.get("name"), path.stem)
        description = _one_line(meta.get("description"), body.splitlines()[0] if body else path.stem)
        mem_type = _one_line(meta.get("type"), "reference")
        if mem_type not in MEMORY_TYPES:
            mem_type = "reference"

        memories.append({
            "filename": path.name,
            "name": name,
            "description": description,
            "type": mem_type,
            "body": body.strip(),
        })
    return memories


def _rebuild_memory_index() -> None:
    """Rebuild MEMORY.md from current memory files."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"- [{memory['name']}]({memory['filename']}) — "
        f"{memory['description']} ({memory['type']})"
        for memory in list_memory_files()
    ]
    MEMORY_INDEX.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def write_memory_file(name: str, mem_type: str, description: str, body: str) -> Path:
    """Write one durable memory and refresh MEMORY.md."""
    mem_type = _one_line(mem_type, "reference")
    if mem_type not in MEMORY_TYPES:
        mem_type = "reference"

    safe_name = _one_line(name, "memory")
    safe_description = _one_line(description, safe_name)
    safe_body = str(body or safe_description).strip()
    filepath = MEMORY_DIR / f"{_slugify_memory_name(safe_name)}.md"

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    filepath.write_text(
        "---\n"
        f"name: {safe_name}\n"
        f"description: {safe_description}\n"
        f"type: {mem_type}\n"
        "---\n\n"
        f"{safe_body}\n",
        encoding="utf-8",
    )
    _rebuild_memory_index()
    return filepath


def _json_array_from_text(text: str) -> list:
    """Extract a JSON array from raw model output."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", stripped, flags=re.S)
        if not match:
            return []
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, list) else []


def _fallback_select_relevant_memories(messages: list, files: list[dict], max_items: int) -> list[str]:
    """Fallback selector: score name/description by recent dialogue keywords."""
    recent = format_recent_messages(messages[-8:], max_chars=3000).lower()
    terms = set(re.findall(r"[a-z0-9_-]{3,}|[\u4e00-\u9fff]{2,}", recent))
    if not terms:
        return []

    scored = []
    for memory in files:
        haystack = f"{memory['name']} {memory['description']} {memory['type']}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, memory["filename"]))

    scored.sort(reverse=True)
    return [filename for _, filename in scored[:max_items]]


def select_relevant_memories(messages: list, max_items: int = MAX_LOADED_MEMORIES) -> list[str]:
    """Ask a cheap side-query to select relevant memory filenames."""
    files = list_memory_files()
    if not files:
        return []

    recent = format_recent_messages(messages[-8:], max_chars=4000)
    catalog = "\n".join(
        f"{index}: {memory['name']} — {memory['description']} ({memory['type']})"
        for index, memory in enumerate(files)
    )
    prompt = (
        "Select persistent memories relevant to the recent conversation. "
        "Return only a JSON array of integer indices, with no prose. "
        f"Select at most {max_items}. Return [] if none are relevant.\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )

    try:
        response = get_client().messages.create(
            model=MODEL,
            system="You select relevant persistent memories for a coding agent.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        indices = _json_array_from_text(extract_text(response.content))
        selected = []
        for raw_index in indices:
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(files):
                selected.append(files[index]["filename"])
            if len(selected) >= max_items:
                break
        return selected
    except Exception as e:
        print(f"\033[90m[Memory: selector fallback: {e}]\033[0m")
        return _fallback_select_relevant_memories(messages, files, max_items)


def load_memories(messages: list, max_items: int = MAX_LOADED_MEMORIES) -> list:
    """Inject selected full memories as a temporary user turn."""
    filenames = select_relevant_memories(messages, max_items=max_items)
    if not filenames:
        return messages

    loaded = []
    for filename in filenames:
        try:
            path = _memory_file_path(filename)
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="replace").strip()
                loaded.append(f"### {filename}\n{content[:8000]}")
        except (OSError, ValueError) as e:
            loaded.append(f"### {filename}\n[Memory failed to load: {e}]")

    if not loaded:
        return messages

    print(f"\033[90m[Memory: loaded {len(loaded)} relevant memories]\033[0m")
    loaded_text = "\n\n".join(loaded)
    memory_turn = {
        "role": "user",
        "content": (
            "<persistent_memories>\n"
            "These are durable memories selected for the current turn. Apply them when relevant.\n\n"
            f"{loaded_text}\n"
            "</persistent_memories>"
        ),
    }
    return [*messages, memory_turn]


def _memory_duplicate_key(value: str) -> str:
    """Normalize names/descriptions for a cheap duplicate guard."""
    return re.sub(r"\W+", "", str(value).lower())


def extract_memories(messages: list) -> int:
    """Extract new durable memories after a turn has naturally stopped."""
    existing_files = list_memory_files()
    existing = "\n".join(
        f"- {memory['name']}: {memory['description']} ({memory['type']})"
        for memory in existing_files
    ) or "- (none)"
    dialogue = format_recent_messages(messages[-10:], max_chars=4000)
    prompt = (
        "Extract durable memories for a coding agent. Save only information that will "
        "still be useful in future sessions: stable user preferences, repeated feedback, "
        "project facts, or references to where things live.\n\n"
        "Allowed type values: user, feedback, project, reference.\n"
        "Return only a JSON array with objects shaped exactly like: "
        "[{\"name\":\"short-kebab-or-title\",\"type\":\"user\",\"description\":\"one line\","
        "\"body\":\"markdown body with why/how to apply when useful\"}].\n"
        "If the information is already covered by Existing memories, or there is nothing "
        "durable to save, return []. Do not save one-off task details.\n\n"
        f"Existing memories:\n{existing}\n\n"
        f"Dialogue:\n{dialogue}"
    )

    try:
        response = get_client().messages.create(
            model=MODEL,
            system="You extract durable long-term memories for a coding agent.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
        )
        candidates = _json_array_from_text(extract_text(response.content))
    except Exception as e:
        print(f"\033[90m[Memory: extraction skipped: {e}]\033[0m")
        return 0

    existing_keys = {
        _memory_duplicate_key(memory["name"]) for memory in existing_files
    } | {
        _memory_duplicate_key(memory["description"]) for memory in existing_files
    }

    written = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        name = _one_line(candidate.get("name"), "")
        description = _one_line(candidate.get("description"), name)
        body = str(candidate.get("body") or description).strip()
        mem_type = _one_line(candidate.get("type"), "reference")
        if not name or not description or not body:
            continue
        if mem_type not in MEMORY_TYPES:
            mem_type = "reference"

        if (
            _memory_duplicate_key(name) in existing_keys
            or _memory_duplicate_key(description) in existing_keys
        ):
            continue

        write_memory_file(name=name, mem_type=mem_type, description=description, body=body)
        existing_keys.add(_memory_duplicate_key(name))
        existing_keys.add(_memory_duplicate_key(description))
        written += 1

    print(f"\033[90m[Memory: extracted {written} new memories]\033[0m")
    return written


def consolidate_memories(threshold: int = CONSOLIDATE_THRESHOLD) -> int:
    """Consolidate memory files when the file count reaches the threshold."""
    files = list_memory_files()
    if len(files) < threshold:
        return 0

    all_memories = []
    for memory in files:
        try:
            path = _memory_file_path(memory["filename"])
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            all_memories.append(f"### {memory['filename']}\n{content}")
        except (OSError, ValueError):
            continue

    all_memories_text = "\n\n".join(all_memories)[:20000]
    prompt = (
        "Consolidate these persistent memories. Deduplicate overlapping items, resolve "
        "conflicts by keeping the most recent or most specific durable fact, and remove "
        "obsolete or one-off task notes.\n\n"
        "Return only a JSON array of objects shaped exactly like: "
        "[{\"name\":\"short-kebab-or-title\",\"type\":\"user\",\"description\":\"one line\","
        "\"body\":\"markdown body\"}].\n\n"
        f"Memories:\n{all_memories_text}"
    )

    try:
        response = get_client().messages.create(
            model=MODEL,
            system="You consolidate persistent memories for a coding agent.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
        )
        consolidated = _json_array_from_text(extract_text(response.content))
    except Exception as e:
        print(f"\033[90m[Memory: consolidation skipped: {e}]\033[0m")
        return 0

    valid = []
    for item in consolidated:
        if not isinstance(item, dict):
            continue
        name = _one_line(item.get("name"), "")
        description = _one_line(item.get("description"), name)
        body = str(item.get("body") or description).strip()
        mem_type = _one_line(item.get("type"), "reference")
        if name and description and body:
            valid.append({
                "name": name,
                "type": mem_type if mem_type in MEMORY_TYPES else "reference",
                "description": description,
                "body": body,
            })

    if not valid:
        return 0

    for memory in files:
        try:
            _memory_file_path(memory["filename"]).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
    for item in valid:
        write_memory_file(
            name=item["name"],
            mem_type=item["type"],
            description=item["description"],
            body=item["body"],
        )

    print(f"\033[90m[Memory: consolidated {len(files)} -> {len(valid)} memories]\033[0m")
    return len(valid)

