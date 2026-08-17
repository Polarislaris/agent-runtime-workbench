"""Git worktree tools for s18 directory isolation.

The database layer records worktree state; this module performs the external Git
side effects. SQLite cannot roll back Git commands, so create_worktree uses a
small compensation step if DB recording fails after Git succeeds.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from ..config import WORKDIR, WORKTREES_DIR
from ..database.worktrees import WORKTREE_STATUSES, WORKTREE_STORE, WorktreeRecord
from ..runtime.domain_events import emit_domain_event


WORKTREE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def validate_worktree_name(name: str) -> str:
    """Return a safe worktree name or raise ValueError.

    Worktree names become both directory names and branch suffixes, so keep the
    allowed character set intentionally small and reject path traversal.
    """
    normalized = str(name).strip()
    if not WORKTREE_NAME_RE.match(normalized):
        raise ValueError("worktree name must match [A-Za-z0-9._-]{1,64}")
    if normalized in {".", ".."}:
        raise ValueError("worktree name must not be . or ..")
    return normalized


def _safe_worktree_path(name: str) -> Path:
    """Build a path under WORKTREES_DIR and reject escaped paths."""
    normalized = validate_worktree_name(name)
    root = WORKTREES_DIR.resolve()
    path = (root / normalized).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"worktree path escapes WORKTREES_DIR: {name}")
    return path


def _run_git(args: list[str], cwd: Optional[Path] = None) -> tuple[bool, str]:
    """Run git with argv arguments and return success plus combined output."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)

    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output or "(no output)"


def _ensure_git_repo() -> tuple[bool, str]:
    """Verify that WORKDIR is a local Git repository."""
    ok, output = _run_git(["rev-parse", "--show-toplevel"], cwd=WORKDIR)
    if not ok:
        return (
            False,
            "Error: worktree isolation requires WORKDIR to be a local git repository. "
            "Run git init and create an initial commit before create_worktree.",
        )
    repo_root = Path(output.splitlines()[-1]).resolve()
    workdir = WORKDIR.resolve()
    if not (workdir == repo_root or workdir.is_relative_to(repo_root)):
        return False, f"Error: WORKDIR is not inside git repo root: {repo_root}"
    return True, str(repo_root)


def _cleanup_created_worktree(path: Path, branch: str) -> None:
    """Best-effort cleanup after Git succeeded but DB recording failed."""
    _run_git(["worktree", "remove", str(path), "--force"], cwd=WORKDIR)
    _run_git(["branch", "-D", branch], cwd=WORKDIR)


def _worktree_has_changes(path: Path) -> tuple[bool, str]:
    """Return whether a worktree has uncommitted changes."""
    ok, output = _run_git(["status", "--porcelain"], cwd=path)
    if not ok:
        return True, output
    return bool(output.strip()), output


def _record_json(record: WorktreeRecord) -> str:
    """Render worktree state as stable JSON for tool results."""
    return json.dumps(record.to_dict(), ensure_ascii=False, indent=2)


def _emit_worktree_event(event_type: str, record: WorktreeRecord) -> None:
    """Emit stable references after the matching worktree transaction commits."""
    emit_domain_event(event_type, {
        "worktree_name": record.worktree_name,
        "task_id": record.task_id,
        "status": record.status,
        "branch": record.branch,
    })


def create_worktree(name: str, task_id: str = "") -> str:
    """Tool handler: create a Git worktree and optionally bind it to a task."""
    try:
        normalized = validate_worktree_name(name)
        path = _safe_worktree_path(normalized)
    except ValueError as exc:
        return f"Error: {exc}"

    ok, repo_or_error = _ensure_git_repo()
    if not ok:
        return repo_or_error

    if path.exists():
        return f"Error: worktree path already exists: {path}"

    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    branch = f"wt/{normalized}"
    ok, output = _run_git(
        ["worktree", "add", str(path), "-b", branch, "HEAD"],
        cwd=WORKDIR,
    )
    if not ok:
        return f"Git error: {output}"

    try:
        record = WORKTREE_STORE.create_record(
            normalized,
            path=path,
            branch=branch,
            task_id=task_id,
        )
    except Exception as exc:
        _cleanup_created_worktree(path, branch)
        return f"Error: created git worktree but failed to record it; cleaned up: {exc}"

    _emit_worktree_event("worktree.created", record)
    return _record_json(record)


def bind_task_to_worktree(task_id: str, worktree_name: str) -> str:
    """Tool handler: bind an existing active worktree to a pending task."""
    try:
        normalized = validate_worktree_name(worktree_name)
        record = WORKTREE_STORE.bind_task(task_id=task_id, worktree_name=normalized)
        _emit_worktree_event("worktree.bound", record)
        return _record_json(record)
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"


def list_worktrees(status: Optional[str] = None) -> str:
    """Tool handler: list known worktrees, optionally by status."""
    try:
        normalized_status = str(status).strip() if status is not None else ""
        if normalized_status and normalized_status not in WORKTREE_STATUSES:
            return f"Error: invalid worktree status: {normalized_status}"
        records = WORKTREE_STORE.list_worktrees(status=normalized_status or None)
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"

    if not records:
        return "(no worktrees)"
    return "\n".join(_record_json(record) for record in records)


def keep_worktree(name: str) -> str:
    """Tool handler: mark a worktree as kept for review."""
    try:
        normalized = validate_worktree_name(name)
        record = WORKTREE_STORE.keep_worktree(normalized)
        _emit_worktree_event("worktree.kept", record)
        return _record_json(record)
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    """Tool handler: remove a Git worktree after safety checks."""
    try:
        normalized = validate_worktree_name(name)
        record = WORKTREE_STORE.get_worktree(normalized)
        if not record:
            return f"Error: Worktree not found: {normalized}"

        path = Path(record.path).resolve()
        root = WORKTREES_DIR.resolve()
        if not path.is_relative_to(root):
            return f"Error: refusing to remove path outside WORKTREES_DIR: {path}"

        has_changes, details = _worktree_has_changes(path)
        if has_changes and not discard_changes:
            return (
                "Error: worktree has uncommitted changes. "
                "Use keep_worktree for review or remove_worktree(discard_changes=true) "
                f"to discard them.\n{details}"
            )

        ok, output = _run_git(["worktree", "remove", str(path), "--force"], cwd=WORKDIR)
        if not ok:
            WORKTREE_STORE.mark_failed(normalized, output)
            failed = WORKTREE_STORE.get_worktree(normalized)
            if failed:
                _emit_worktree_event("worktree.failed", failed)
            return f"Git error: {output}"

        branch_ok, branch_output = _run_git(["branch", "-D", record.branch], cwd=WORKDIR)
        if not branch_ok:
            WORKTREE_STORE.mark_failed(normalized, branch_output)
            failed = WORKTREE_STORE.get_worktree(normalized)
            if failed:
                _emit_worktree_event("worktree.failed", failed)
            return f"Git branch cleanup error: {branch_output}"

        removed = WORKTREE_STORE.mark_removed(normalized)
        _emit_worktree_event("worktree.removed", removed)
        return _record_json(removed)
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"


def get_task_worktree(task_id: str) -> Optional[WorktreeRecord]:
    """Return the worktree bound to a task, if any."""
    return WORKTREE_STORE.get_task_worktree(task_id)


def list_worktree_events(
    worktree_name: Optional[str] = None,
    task_id: Optional[str] = None,
    limit: int = 50,
) -> str:
    """Tool handler: inspect recent worktree lifecycle events."""
    try:
        normalized_name = validate_worktree_name(worktree_name) if worktree_name else None
        events = WORKTREE_STORE.list_worktree_events(
            worktree_name=normalized_name,
            task_id=task_id,
            limit=limit,
        )
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"

    if not events:
        return "(no worktree events)"
    return "\n".join(
        json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        for event in events
    )
