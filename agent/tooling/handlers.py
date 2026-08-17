"""Dispatch table that maps model tool names to Python handlers."""

from __future__ import annotations

from ..features.cron_scheduler import cancel_cron, list_crons, schedule_cron
from ..tooling.fs import run_bash, run_edit, run_glob, run_read, run_write
from ..features.skills import load_skill
from ..features.subagent import spawn_subagent
from ..features.task_system import (
    claim_task,
    complete_task,
    create_task,
    get_task,
    list_task_events,
    list_tasks,
)
from ..features.team import list_teammates, run_check_inbox, run_send_message, spawn_teammate_thread
from ..database.team_protocols import (
    list_protocol_requests,
    request_plan,
    request_shutdown,
    review_plan,
    submit_plan,
)
from ..features.todos import run_todo_write
from ..features.worktree import (
    bind_task_to_worktree,
    create_worktree,
    keep_worktree,
    list_worktree_events,
    list_worktrees,
    remove_worktree,
)
from ..features.worktree_review import (
    commit_worktree,
    diff_worktree,
    list_worktree_checks,
    list_worktree_merges,
    list_worktree_reviews,
    merge_worktree,
    prepare_merge_worktree,
    review_worktree,
    test_worktree,
)
from ..features.mcp import connect_mcp


# Builtin handlers are static. ``assemble_tool_pool`` combines them with the
# handlers generated for connected mock MCP tools.
BUILTIN_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "todo_write": run_todo_write,
    "task": spawn_subagent,
    "load_skill": load_skill,
    "create_task": create_task,
    "list_tasks": list_tasks,
    "get_task": get_task,
    "claim_task": claim_task,
    "complete_task": complete_task,
    "list_task_events": list_task_events,
    "create_worktree": create_worktree,
    "bind_task_to_worktree": bind_task_to_worktree,
    "list_worktrees": list_worktrees,
    "keep_worktree": keep_worktree,
    "remove_worktree": remove_worktree,
    "list_worktree_events": list_worktree_events,
    "diff_worktree": diff_worktree,
    "review_worktree": review_worktree,
    "test_worktree": test_worktree,
    "commit_worktree": commit_worktree,
    "prepare_merge_worktree": prepare_merge_worktree,
    "merge_worktree": merge_worktree,
    "list_worktree_reviews": list_worktree_reviews,
    "list_worktree_checks": list_worktree_checks,
    "list_worktree_merges": list_worktree_merges,
    "schedule_cron": schedule_cron,
    "list_crons": list_crons,
    "cancel_cron": cancel_cron,
    "spawn_teammate": spawn_teammate_thread,
    "send_message": run_send_message,
    "check_inbox": run_check_inbox,
    "list_teammates": list_teammates,
    "request_shutdown": request_shutdown,
    "request_plan": request_plan,
    "review_plan": review_plan,
    "list_protocol_requests": list_protocol_requests,
    "submit_plan": submit_plan,
    "connect_mcp": connect_mcp,
}

# Compatibility export. The Lead loop obtains the dynamic version from
# tooling.pool; keeping this name preserves existing imports in teaching code.
TOOL_HANDLERS = BUILTIN_HANDLERS
