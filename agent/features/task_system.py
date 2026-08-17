"""Persistent SQLite task system for project-level work.

s12 introduced a file-backed `.tasks` directory. s17 upgrades that task board to
SQLite so autonomous teammates can scan and claim work concurrently. This module
keeps the public tool functions stable while delegating all state transitions to
`database.autonomous_tasks`, where multi-table updates are wrapped in
transactions.
"""

from __future__ import annotations

import json
from typing import Optional

from ..database.autonomous_tasks import TASK_STATUSES, TASK_STORE, TaskRecord
from ..runtime.domain_events import emit_domain_event


def _task_event_payload(task: TaskRecord) -> dict:
    """Keep browser task events tied to durable IDs instead of tool prose."""
    return {
        "task_id": task.task_id,
        "owner": task.owner,
        "status": task.status,
        "blocked_by": list(task.blockedBy),
        "worktree_name": task.worktree_name,
    }


def _task_json(task: TaskRecord) -> str:
    """Render one task as readable JSON for get/create tool results."""
    return json.dumps(task.to_dict(), ensure_ascii=False, indent=2)


def _format_task_line(task: TaskRecord) -> str:
    """Render one compact task-board row for list_tasks."""
    owner = f" owner={task.owner}" if task.owner else ""
    worktree = f" worktree={task.worktree_name}" if task.worktree_name else ""
    deps = f" blockedBy={task.blockedBy}" if task.blockedBy else ""
    priority = f" priority={task.priority}" if task.priority else ""
    return f"- {task.task_id} [{task.status}]{owner}{worktree}{priority}{deps}: {task.subject}"


def create_task(
    subject: str,
    description: str = "",
    blockedBy: Optional[list[str]] = None,
    priority: int = 0,
) -> str:
    """Tool handler: create a pending SQLite task and dependency rows."""
    try:
        task = TASK_STORE.create_task(
            subject=subject,
            description=description,
            blockedBy=blockedBy,
            priority=priority,
        )
        emit_domain_event("task.created", _task_event_payload(task))
        return _task_json(task)
    except (OSError, ValueError, RuntimeError) as exc:
        return f"Error: {exc}"


def list_tasks(status: Optional[str] = None, owner: Optional[str] = None) -> str:
    """Tool handler: list task summaries, optionally filtered by status/owner."""
    try:
        normalized_status = str(status).strip() if status is not None else ""
        if normalized_status and normalized_status not in TASK_STATUSES:
            return f"Error: invalid status: {normalized_status}"
        tasks = TASK_STORE.list_tasks(
            status=normalized_status or None,
            owner=owner,
        )
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"

    if not tasks:
        return "(no tasks)"
    return "\n".join(_format_task_line(task) for task in tasks)


def get_task(task_id: str) -> str:
    """Tool handler: return full JSON for one SQLite-backed task."""
    try:
        task = TASK_STORE.get_task(task_id)
        if not task:
            return f"Error: Task not found: {task_id}"
        return _task_json(task)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return f"Error: {exc}"


def blocked_dependency_ids(task: TaskRecord | str) -> list[str]:
    """Return dependency ids that are missing completion.

    The public helper accepts either a TaskRecord or task_id to keep old tests
    and callers simple while the storage implementation moves to SQLite.
    """
    task_id = task.task_id if isinstance(task, TaskRecord) else str(task)
    return TASK_STORE.blocked_dependency_ids(task_id)


def can_start(task_id: str) -> bool:
    """Return True only when every blockedBy dependency is completed."""
    return TASK_STORE.can_start(task_id)


def scan_unclaimed_tasks(limit: int = 5) -> list[dict]:
    """Return pending, unowned, unblocked tasks for autonomous teammates."""
    try:
        return [task.to_dict() for task in TASK_STORE.scan_unclaimed_tasks(limit=limit)]
    except (OSError, ValueError):
        return []


def claim_task(task_id: str, owner: str = "lead") -> str:
    """Tool handler: atomically move a pending unblocked task to in_progress."""
    try:
        task = TASK_STORE.claim_task(task_id, owner=owner or "lead")
        emit_domain_event("task.claimed", _task_event_payload(task))
        return f"Claimed {task.task_id} ({task.subject}) for {task.owner}"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return f"Error: {exc}"


def complete_task(task_id: str, owner: Optional[str] = None) -> str:
    """Tool handler: mark an in-progress task completed.

    When a teammate calls this tool, team.py supplies owner=<teammate name> so
    only the owner can complete the task. Lead/manual calls omit owner for
    backward compatibility and act as an administrative completion.
    """
    try:
        task, unblocked = TASK_STORE.complete_task(task_id, owner=owner)
        completed_payload = _task_event_payload(task)
        completed_payload["unblocked_task_ids"] = [item.task_id for item in unblocked]
        emit_domain_event("task.completed", completed_payload)
        ready_note = ""
        if task.worktree_name:
            try:
                from ..database.worktrees import WORKTREE_STORE

                ready = WORKTREE_STORE.mark_ready_for_review(task.task_id)
                if ready:
                    ready_note = f"\nWorktree ready_for_review: {ready.worktree_name}"
            except Exception as exc:
                ready_note = f"\nWarning: failed to mark worktree ready_for_review: {exc}"
        if task.status == "completed" and not unblocked:
            return f"Completed {task.task_id} ({task.subject}){ready_note}"

        message = f"Completed {task.task_id} ({task.subject})"
        if unblocked:
            names = ", ".join(f"{item.task_id} ({item.subject})" for item in unblocked)
            message += f"\nUnblocked: {names}"
        message += ready_note
        return message
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return f"Error: {exc}"


def list_task_events(
    task_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    limit: int = 50,
) -> str:
    """Tool handler: inspect recent task lifecycle audit events."""
    try:
        events = TASK_STORE.list_task_events(
            task_id=task_id,
            agent_id=agent_id,
            limit=limit,
        )
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"

    if not events:
        return "(no task events)"
    return "\n".join(
        json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        for event in events
    )
