"""Lead-side review, commit, and merge tools for isolated Git worktrees."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Optional

from ..config import WORKDIR, WORKTREES_DIR
from ..database.worktree_reviews import WORKTREE_REVIEW_STORE
from ..database.worktrees import WORKTREE_STORE, WorktreeRecord
from ..runtime.domain_events import emit_domain_event
from .worktree import _ensure_git_repo, _run_git, validate_worktree_name


MERGEABLE_STATUSES = {"approved", "committed"}
REVIEWABLE_STATUSES = {"ready_for_review", "needs_changes", "approved"}
COMMITTABLE_STATUSES = {"ready_for_review", "approved"}


def _json(data: dict) -> str:
    """Render stable JSON output for model-readable tool results."""
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _truncate(text: str, max_chars: int) -> str:
    """Keep large Git output inside a bounded tool-result size."""
    limit = max(200, int(max_chars or 12000))
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[truncated: {len(text) - limit} chars omitted]"


def _load_record(name: str) -> WorktreeRecord:
    """Validate a worktree name and load its DB record."""
    normalized = validate_worktree_name(name)
    record = WORKTREE_STORE.get_worktree(normalized)
    if not record:
        raise ValueError(f"Worktree not found: {normalized}")
    return record


def _safe_worktree_dir(record: WorktreeRecord) -> Path:
    """Return the worktree path after confirming it stays in WORKTREES_DIR."""
    path = Path(record.path).resolve()
    root = WORKTREES_DIR.resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"refusing to operate outside WORKTREES_DIR: {path}")
    if not path.exists() or not path.is_dir():
        raise ValueError(f"worktree path does not exist: {path}")
    return path


def _git_output(args: list[str], cwd: Path) -> str:
    """Run Git and raise on failure for internal helper flows."""
    ok, output = _run_git(args, cwd=cwd)
    if not ok:
        raise ValueError(output)
    return output


def _diff_snapshot(record: WorktreeRecord, include_patch: bool, max_chars: int) -> dict:
    """Collect a read-only diff snapshot from the worktree branch."""
    path = _safe_worktree_dir(record)
    status = _git_output(["status", "--short"], cwd=path)
    stat = _git_output(["diff", "--stat"], cwd=path)
    names = _git_output(["diff", "--name-only"], cwd=path)
    snapshot = {
        "worktree_name": record.worktree_name,
        "task_id": record.task_id,
        "status": record.status,
        "path": str(path),
        "branch": record.branch,
        "git_status_short": "" if status == "(no output)" else status,
        "git_diff_stat": "" if stat == "(no output)" else stat,
        "git_diff_name_only": "" if names == "(no output)" else names,
    }
    if include_patch:
        patch = _git_output(["diff"], cwd=path)
        snapshot["git_diff"] = _truncate("" if patch == "(no output)" else patch, max_chars)
    return snapshot


def read_worktree_diff(name: str, max_chars: int = 12000) -> dict:
    """Read a worktree diff for the HTTP API without creating a tool audit event.

    The interactive ``diff_worktree`` tool below intentionally records that a
    Lead inspected the diff.  A browser redraw, however, must remain read-only
    and must not mutate review history merely because a drawer was opened.
    """
    record = _load_record(name)
    return _diff_snapshot(record, include_patch=True, max_chars=int(max_chars))


def _diff_summary(snapshot: dict) -> str:
    """Create compact text that is safe to persist in the review table."""
    parts = [
        "status --short:",
        snapshot.get("git_status_short") or "(clean)",
        "diff --stat:",
        snapshot.get("git_diff_stat") or "(no diff stat)",
        "diff --name-only:",
        snapshot.get("git_diff_name_only") or "(no changed files)",
    ]
    return "\n".join(parts)


def _run_shell(command: str, cwd: Path) -> tuple[int, str]:
    """Run a verification command inside the worktree and capture output."""
    try:
        result = subprocess.run(
            str(command),
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, str(exc)
    output = (result.stdout + result.stderr).strip()
    return int(result.returncode), output or "(no output)"


def diff_worktree(name: str, include_patch: bool = False, max_chars: int = 12000) -> str:
    """Tool handler: inspect worktree changes without mutating Git state."""
    try:
        record = _load_record(name)
        snapshot = _diff_snapshot(record, bool(include_patch), int(max_chars or 12000))
        WORKTREE_STORE.insert_event(
            record.worktree_name,
            record.task_id,
            "diffed",
            _diff_summary(snapshot),
        )
        emit_domain_event("worktree.diffed", {
            "worktree_name": record.worktree_name,
            "task_id": record.task_id,
            "status": record.status,
        })
        return _json(snapshot)
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"


def review_worktree(
    name: str,
    approve: bool,
    summary: str = "",
    notes: str = "",
) -> str:
    """Tool handler: record Lead review and move worktree to approved/needs_changes."""
    try:
        record = _load_record(name)
        if record.status not in REVIEWABLE_STATUSES:
            return f"Error: Worktree {record.worktree_name} is {record.status}, cannot review"
        snapshot = _diff_snapshot(record, include_patch=False, max_chars=12000)
        review = WORKTREE_REVIEW_STORE.record_review(
            record.worktree_name,
            reviewer="lead",
            approve=bool(approve),
            summary=summary,
            notes=notes,
            diff_summary=_diff_summary(snapshot),
        )
        emit_domain_event("worktree.reviewed", {
            "worktree_name": review.worktree_name,
            "task_id": review.task_id,
            "review_id": review.review_id,
            "status": review.status,
        })
        return _json(review.to_dict())
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"


def test_worktree(name: str, command: str) -> str:
    """Tool handler: run a check command inside an isolated worktree."""
    try:
        record = _load_record(name)
        path = _safe_worktree_dir(record)
        command_text = str(command or "").strip()
        if not command_text:
            return "Error: command is required"
        exit_code, output = _run_shell(command_text, cwd=path)
        check = WORKTREE_REVIEW_STORE.record_check(
            record.worktree_name,
            command=command_text,
            exit_code=exit_code,
            output_preview=_truncate(output, 8000),
        )
        emit_domain_event("worktree.checked", {
            "worktree_name": check.worktree_name,
            "task_id": check.task_id,
            "check_id": check.check_id,
            "status": check.status,
            "exit_code": check.exit_code,
        })
        return _json({
            "check": check.to_dict(),
            "output": _truncate(output, 12000),
        })
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"


def commit_worktree(name: str, message: str) -> str:
    """Tool handler: create a Git commit from worktree changes and record it."""
    git_commit_succeeded = False
    try:
        record = _load_record(name)
        if record.status not in COMMITTABLE_STATUSES:
            return (
                f"Error: Worktree {record.worktree_name} is {record.status}; "
                "commit requires ready_for_review or approved"
            )
        message_text = str(message or "").strip()
        if not message_text:
            return "Error: commit message is required"
        path = _safe_worktree_dir(record)
        status = _git_output(["status", "--porcelain"], cwd=path)
        if not status.strip() or status == "(no output)":
            return "Error: no worktree changes to commit"

        ok, add_output = _run_git(["add", "."], cwd=path)
        if not ok:
            return f"Git add error: {add_output}"
        ok, commit_output = _run_git(["commit", "-m", message_text], cwd=path)
        if not ok:
            return f"Git commit error: {commit_output}"
        git_commit_succeeded = True
        commit_sha = _git_output(["rev-parse", "HEAD"], cwd=path).splitlines()[-1]
        commit = WORKTREE_REVIEW_STORE.record_commit(
            record.worktree_name,
            commit_sha=commit_sha,
            commit_message=message_text,
        )
        emit_domain_event("worktree.committed", {
            "worktree_name": commit.worktree_name,
            "task_id": commit.task_id,
            "commit_id": commit.commit_id,
            "commit_sha": commit.commit_sha,
        })
        return _json({
            "commit": commit.to_dict(),
            "git_output": commit_output,
        })
    except (OSError, ValueError) as exc:
        if git_commit_succeeded:
            return f"Error: Git commit succeeded but DB recording failed: {exc}"
        return f"Error: {exc}"
    except Exception as exc:
        # Git commits are not rolled back automatically because reset is
        # destructive. Surface the mismatch so a later reconcile tool can fix DB.
        return f"Error: Git commit may have succeeded but DB recording failed: {exc}"


def prepare_merge_worktree(name: str, target_branch: str = "main") -> str:
    """Tool handler: validate and persist a merge plan without executing merge."""
    try:
        record = _load_record(name)
        if record.status not in MERGEABLE_STATUSES:
            return (
                f"Error: Worktree {record.worktree_name} is {record.status}; "
                "prepare merge requires approved or committed"
            )
        ok, repo_or_error = _ensure_git_repo()
        if not ok:
            return repo_or_error
        target = str(target_branch or "main").strip()
        if not target:
            return "Error: target_branch is required"
        source_commit = _git_output(["rev-parse", record.branch], cwd=WORKDIR).splitlines()[-1]
        target_before = _git_output(["rev-parse", target], cwd=WORKDIR).splitlines()[-1]
        main_status = _git_output(["status", "--porcelain"], cwd=WORKDIR)
        if main_status.strip() and main_status != "(no output)":
            return f"Error: WORKDIR must be clean before merge planning:\n{main_status}"
        plan = (
            f"Merge worktree {record.worktree_name} branch {record.branch} "
            f"({source_commit}) into {target} currently at {target_before}. "
            "This prepare step does not change branches or merge code."
        )
        merge = WORKTREE_REVIEW_STORE.prepare_merge(
            record.worktree_name,
            target_branch=target,
            source_commit=source_commit,
            target_before_commit=target_before,
            plan=plan,
        )
        emit_domain_event("worktree.merge_prepared", {
            "worktree_name": merge.worktree_name,
            "task_id": merge.task_id,
            "merge_id": merge.merge_id,
            "status": merge.status,
            "target_branch": merge.target_branch,
        })
        return _json(merge.to_dict())
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"


def merge_worktree(
    name: str,
    target_branch: str = "main",
    user_confirmed: bool = False,
) -> str:
    """Tool handler: merge an approved/committed worktree after explicit confirmation."""
    if not bool(user_confirmed):
        return "Error: merge_worktree requires explicit user confirmation"

    try:
        record = _load_record(name)
        if record.status not in MERGEABLE_STATUSES:
            return (
                f"Error: Worktree {record.worktree_name} is {record.status}; "
                "merge requires approved or committed"
            )
        ok, repo_or_error = _ensure_git_repo()
        if not ok:
            return repo_or_error
        target = str(target_branch or "main").strip()
        if not target:
            return "Error: target_branch is required"
        main_status = _git_output(["status", "--porcelain"], cwd=WORKDIR)
        if main_status.strip() and main_status != "(no output)":
            return f"Error: WORKDIR must be clean before merge:\n{main_status}"

        ok, checkout_output = _run_git(["checkout", target], cwd=WORKDIR)
        if not ok:
            WORKTREE_REVIEW_STORE.record_merge_failure(
                record.worktree_name,
                target_branch=target,
                error=checkout_output,
            )
            return f"Git checkout error: {checkout_output}"

        ok, merge_output = _run_git(["merge", "--no-ff", record.branch], cwd=WORKDIR)
        if not ok:
            WORKTREE_REVIEW_STORE.record_merge_failure(
                record.worktree_name,
                target_branch=target,
                error=merge_output,
            )
            return (
                "Git merge error. Resolve conflicts manually or explicitly abort the "
                f"merge outside this tool.\n{merge_output}"
            )

        merge_commit = _git_output(["rev-parse", "HEAD"], cwd=WORKDIR).splitlines()[-1]
        merge = WORKTREE_REVIEW_STORE.record_merge_success(
            record.worktree_name,
            target_branch=target,
            merge_commit=merge_commit,
        )
        emit_domain_event("worktree.merged", {
            "worktree_name": merge.worktree_name,
            "task_id": merge.task_id,
            "merge_id": merge.merge_id,
            "status": merge.status,
            "target_branch": merge.target_branch,
        })
        return _json({
            "merge": merge.to_dict(),
            "checkout_output": checkout_output,
            "merge_output": merge_output,
        })
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"


def list_worktree_reviews(
    worktree_name: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Tool handler: list stored review records."""
    try:
        name = validate_worktree_name(worktree_name) if worktree_name else ""
        records = WORKTREE_REVIEW_STORE.list_reviews(name, limit=limit)
        if not records:
            return "(no worktree reviews)"
        return "\n".join(_json(record.to_dict()) for record in records)
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"


def list_worktree_checks(
    worktree_name: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Tool handler: list stored worktree test/check records."""
    try:
        name = validate_worktree_name(worktree_name) if worktree_name else ""
        records = WORKTREE_REVIEW_STORE.list_checks(name, limit=limit)
        if not records:
            return "(no worktree checks)"
        return "\n".join(_json(record.to_dict()) for record in records)
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"


def list_worktree_merges(
    worktree_name: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Tool handler: list stored merge plans/results."""
    try:
        name = validate_worktree_name(worktree_name) if worktree_name else ""
        records = WORKTREE_REVIEW_STORE.list_merges(name, limit=limit)
        if not records:
            return "(no worktree merges)"
        return "\n".join(_json(record.to_dict()) for record in records)
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"
