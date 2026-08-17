"""Filesystem and shell tool handlers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from ..config import WORKDIR


def safe_path(path: str, base_dir: Optional[Path | str] = None) -> Path:
    """Resolve a relative path and reject paths outside the selected base dir.

    Lead tools use WORKDIR. Teammate tools may pass a Git worktree path, giving
    each teammate an isolated filesystem view for its claimed task.
    """
    base = Path(base_dir or WORKDIR).resolve()
    resolved = (base / path).resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved


def run_bash(
    command: str,
    run_in_background: bool = False,
    cwd: Optional[Path | str] = None,
) -> str:
    """Run a shell command from WORKDIR or a teammate worktree.

    run_in_background is accepted so the same schema can be used for both
    synchronous and background paths. The synchronous handler ignores it; loop.py
    decides whether to route the call to the background manager first.
    """
    workdir = Path(cwd or WORKDIR).resolve()
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = (result.stdout + result.stderr).strip()
        return output[:50000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def run_read(
    path: str,
    limit: Optional[int] = None,
    base_dir: Optional[Path | str] = None,
) -> str:
    """Read a text file, optionally returning only the first limit lines."""
    try:
        file_path = safe_path(path, base_dir=base_dir)
        if not file_path.is_file():
            return f"Error: Not a file: {path}"

        text = file_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if limit is not None:
            limit = int(limit)
            if limit < 1:
                return "Error: limit must be >= 1"
            if len(lines) > limit:
                remaining = len(lines) - limit
                lines = lines[:limit]
                lines.append(f"... ({remaining} more lines)")

        output = "\n".join(lines)
        return output[:50000] if output else "(empty file)"
    except (OSError, ValueError) as e:
        return f"Error: {e}"


def run_write(path: str, content: str, base_dir: Optional[Path | str] = None) -> str:
    """Write complete text content to a workspace file."""
    try:
        file_path = safe_path(path, base_dir=base_dir)
        if file_path.exists() and file_path.is_dir():
            return f"Error: Cannot write file over directory: {path}"

        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content.encode('utf-8'))} bytes to {path}"
    except (OSError, ValueError) as e:
        return f"Error: {e}"


def run_edit(
    path: str,
    old_text: str,
    new_text: str,
    base_dir: Optional[Path | str] = None,
) -> str:
    """Replace the first exact text occurrence in a workspace file."""
    try:
        if not old_text:
            return "Error: old_text must not be empty"

        file_path = safe_path(path, base_dir=base_dir)
        if not file_path.is_file():
            return f"Error: Not a file: {path}"

        text = file_path.read_text(encoding="utf-8", errors="replace")
        if old_text not in text:
            return "Error: text not found"

        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except (OSError, ValueError) as e:
        return f"Error: {e}"


def run_glob(pattern: str, base_dir: Optional[Path | str] = None) -> str:
    """Find workspace files matching a relative glob pattern."""
    try:
        base = Path(base_dir or WORKDIR).resolve()
        pattern_path = Path(pattern)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            return "Error: glob pattern must stay inside the workspace"

        matches = [
            str(path.resolve().relative_to(base))
            for path in base.glob(pattern)
            if path.resolve().is_relative_to(base)
        ]
        if not matches:
            return "(no matches)"
        return "\n".join(sorted(matches))[:50000]
    except (OSError, ValueError) as e:
        return f"Error: {e}"
