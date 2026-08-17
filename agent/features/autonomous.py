"""Autonomous teammate idle polling for s17.

The team loop owns LLM execution; this module owns the decision made during
IDLE: inbox work first, then already-owned task reminders, then atomic task-board
claiming. Keeping this separate prevents task scheduling rules from spreading
through `team.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Optional

from ..config import (
    AUTONOMOUS_TASK_SCAN_LIMIT,
    TEAM_IDLE_POLL_SECONDS,
    TEAM_IDLE_TIMEOUT_SECONDS,
)
from ..database.autonomous_tasks import TASK_STORE, TaskRecord
from ..runtime.domain_events import emit_domain_event
from ..database.team_protocols import consume_teammate_inbox


@dataclass
class IdlePollResult:
    """Result returned to the teammate loop after one idle polling cycle."""

    action: str
    task_id: Optional[str] = None
    worktree_name: Optional[str] = None
    worktree_path: Optional[str] = None
    message: str = ""


def format_claimed_task_for_prompt(task: TaskRecord) -> str:
    """Build a structured task injection for the teammate's local context."""
    deps = ", ".join(task.blockedBy) if task.blockedBy else "(none)"
    worktree = task.worktree_name or "(none)"
    return (
        "<claimed_task>\n"
        f"task_id: {task.task_id}\n"
        f"subject: {task.subject}\n"
        f"description: {task.description or '(no description)'}\n"
        f"priority: {task.priority}\n"
        f"worktree: {worktree}\n"
        f"blockedBy: {deps}\n\n"
        "Work on this task now. When it is actually finished, call "
        f"complete_task with task_id={task.task_id}.\n"
        "</claimed_task>"
    )


def _format_current_task_reminder(task: TaskRecord) -> str:
    """Remind a teammate about a task it still owns before claiming new work."""
    return (
        "<current_task>\n"
        f"task_id: {task.task_id}\n"
        f"subject: {task.subject}\n"
        f"description: {task.description or '(no description)'}\n\n"
        "You still own this in-progress task. Continue it, or call complete_task "
        "when the work is actually finished.\n"
        "</current_task>"
    )


def idle_poll_for_work(agent_id: str, messages: list) -> IdlePollResult:
    """Poll inbox and task board until work arrives, shutdown arrives, or timeout.

    Scanning is intentionally advisory. If two teammates see the same task, only
    the later `claim_task` transaction decides who owns it.
    """
    deadline = time.time() + TEAM_IDLE_TIMEOUT_SECONDS

    while time.time() < deadline:
        inbox = consume_teammate_inbox(agent_id, messages)
        if inbox.shutdown_requested:
            return IdlePollResult(action="shutdown", message=inbox.text)
        if inbox.has_work:
            return IdlePollResult(action="work", message=inbox.text)

        agent = TASK_STORE.get_agent(agent_id)
        if agent and agent.current_task_id:
            task = TASK_STORE.get_task(agent.current_task_id)
            if task and task.status == "in_progress" and task.owner == agent_id:
                messages.append({
                    "role": "user",
                    "content": _format_current_task_reminder(task),
                })
                TASK_STORE.set_agent_state(agent_id, "running")
                worktree_name = None
                worktree_path = None
                try:
                    from ..database.worktrees import WORKTREE_STORE

                    worktree = WORKTREE_STORE.get_task_worktree(task.task_id)
                    if worktree:
                        worktree_name = worktree.worktree_name
                        worktree_path = worktree.path
                except Exception:
                    pass
                return IdlePollResult(
                    action="work",
                    task_id=task.task_id,
                    worktree_name=worktree_name,
                    worktree_path=worktree_path,
                    message=f"Continuing owned task {task.task_id}.",
                )

        candidates = TASK_STORE.scan_unclaimed_tasks(limit=AUTONOMOUS_TASK_SCAN_LIMIT)
        for candidate in candidates:
            try:
                claimed = TASK_STORE.claim_task(candidate.task_id, owner=agent_id)
            except ValueError:
                # Another teammate may have claimed it after our scan. Try the
                # next candidate instead of treating normal contention as fatal.
                continue

            emit_domain_event("task.claimed", {
                "task_id": claimed.task_id,
                "owner": claimed.owner,
                "status": claimed.status,
                "blocked_by": list(claimed.blockedBy),
                "worktree_name": claimed.worktree_name,
            })

            messages.append({
                "role": "user",
                "content": format_claimed_task_for_prompt(claimed),
            })
            worktree_name = None
            worktree_path = None
            try:
                from ..database.worktrees import WORKTREE_STORE

                worktree = WORKTREE_STORE.get_task_worktree(claimed.task_id)
                if worktree:
                    worktree_name = worktree.worktree_name
                    worktree_path = worktree.path
            except Exception:
                # Worktree lookup is advisory for prompt/cwd routing. The task
                # claim itself remains valid even if this lookup fails.
                pass
            return IdlePollResult(
                action="work",
                task_id=claimed.task_id,
                worktree_name=worktree_name,
                worktree_path=worktree_path,
                message=f"Claimed autonomous task {claimed.task_id}.",
            )

        time.sleep(TEAM_IDLE_POLL_SECONDS)

    return IdlePollResult(action="timeout", message="idle timeout")
