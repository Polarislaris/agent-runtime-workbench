"""Runtime system prompt assembly.

s10 changes the prompt from one hardcoded string into independent sections.
Each model call builds a small context from real runtime state, chooses the
needed sections, and reuses a cached assembled prompt when that state is stable.
"""

from __future__ import annotations

import json

from .features.background_tasks import running_background_count
from .config import MEMORY_INDEX, PROJECT_ROOT, STATE_DIR, TEAM_DB, WORKDIR, WORKTREES_DIR
from .features.cron_scheduler import queued_cron_count, scheduled_cron_count
from .features.skills import list_skills


# Each section is edited independently. Adding future capabilities such as error
# recovery or MCP state should mean adding a section, not rewriting one giant
# prompt string.
PROMPT_SECTIONS = {
    "identity": (
        "You are a coding agent. Use tools to solve tasks. Act, don't explain."
    ),
    "workspace": (
        "Working directory:\n"
        "{workspace}\n"
        "Agent source directory:\n"
        "{project_root}\n"
        "Agent state directory:\n"
        "{state_dir}"
    ),
    "tools": (
        "Enabled tools:\n"
        "{enabled_tools}\n\n"
        "For multi-step tasks, use todo_write before making changes, keep exactly "
        "one task in_progress, and update todos as you complete each step. "
        "Use task for focused investigation or self-contained subtasks that would "
        "otherwise fill the parent conversation with noisy intermediate context. "
        "Use compact when the conversation is getting long or the user asks to compact."
    ),
    "skills": (
        "Skills available:\n"
        "{skills}\n\n"
        "When a task matches a skill, call load_skill with the exact skill name "
        "before relying on that skill's detailed workflow."
    ),
    "tasks": (
        "Persistent task system:\n"
        "Task store: SQLite task board in {team_db}\n"
        "Known tasks: {task_count}\n\n"
        "Use create_task/list_tasks/get_task/claim_task/complete_task/list_task_events "
        "for project-level work that must survive across sessions or has blockedBy dependencies. "
        "Use todo_write only for the current turn's local execution checklist. "
        "Claim a task before doing its implementation work, and complete it only after "
        "the work is actually finished. Autonomous teammates can claim unowned pending "
        "tasks whose dependencies are completed, so for larger batches you can create "
        "durable tasks, spawn teammates, and let them self-organize from the board. "
        "Subagents remain one-shot helpers and do not own task-board lifecycle."
    ),
    "background": (
        "Background task policy:\n"
        "Currently running background tasks: {running_background_tasks}\n\n"
        "Use bash.run_in_background only for slow, independent shell commands. "
        "Task-system blockedBy dependencies take priority over background parallelism. "
        "If a follow-up step depends on a background command's files, output, exit "
        "status, installed packages, built artifacts, or side effects, wait for the "
        "<task_notification> completion before running that follow-up step. "
        "Heuristics may suggest background execution for slow commands, but they do "
        "not prove dependent work is safe to run in parallel."
    ),
    "cron": (
        "Cron scheduler:\n"
        "Scheduled cron jobs: {scheduled_cron_jobs}\n"
        "Queued cron triggers: {queued_cron_triggers}\n\n"
        "Use schedule_cron/list_crons/cancel_cron for time-based work. Cron jobs "
        "produce scheduled prompts; they do not execute shell commands directly. "
        "When a scheduled prompt fires, agent_loop receives it as [Scheduled <id>] "
        "and decides which tools to call. Use durable=True for jobs that should "
        "survive Agent restarts, but remember scheduling only runs while this Agent "
        "process is alive."
    ),
    "team": (
        "Team system:\n"
        "SQLite message bus: {team_db}\n"
        "Active teammates: {active_teammates}\n\n"
        "You are the Lead agent. Use spawn_teammate for independent workstreams "
        "that benefit from separate context, not for small single-step tasks. "
        "Use send_message for follow-up instructions and check_inbox before "
        "depending on teammate results. Team messages are durable SQLite inbox "
        "records; they are acknowledged only after being injected into an agent's "
        "conversation history. Idle teammates check inbox first, then scan the "
        "SQLite task board and atomically claim available work. Do not ask "
        "teammates to spawn other teammates."
    ),
    "team_protocols": (
        "Team protocols:\n"
        "Pending protocol requests: {pending_protocol_requests}\n\n"
        "Use request_shutdown for graceful teammate shutdown instead of treating "
        "text messages as proof that a thread has stopped. Use request_plan to ask "
        "a teammate for a plan, and review_plan to approve or reject a submitted "
        "plan_approval request. Teammates should call submit_plan before high-risk "
        "edits. Protocol requests have request_id values and move pending -> "
        "approved/rejected/expired/failed; do not treat a protocol as approved "
        "until the matching response or review_plan resolves it."
    ),
    "worktrees": (
        "Worktree isolation:\n"
        "Worktrees directory: {worktrees_dir}\n"
        "Known worktrees: {worktree_count}\n\n"
        "PROJECT_ROOT is the Agent source. WORKDIR is the business project. "
        "Use create_worktree for larger parallel tasks that should modify files "
        "in isolated Git worktrees. Bind a pending task to a worktree before "
        "teammates claim it when you want directory isolation. Teammates that "
        "claim a bound task run bash/read/write/edit/glob inside that worktree. "
        "complete_task only means the teammate thinks the task work is done; it "
        "does not mean the code is approved, committed, or merged. After a "
        "worktree-bound task is completed, inspect it with diff_worktree and "
        "optionally test_worktree, then use review_worktree to mark approved or "
        "needs_changes. Use commit_worktree only as Lead after review. Use "
        "prepare_merge_worktree to create an auditable merge plan without "
        "changing branches. merge_worktree changes the target branch and must "
        "only run after explicit user confirmation. keep_worktree/remove_worktree "
        "controls the directory lifecycle after review/merge decisions. Do not "
        "remove worktrees with changes unless the user explicitly allows "
        "discard_changes."
    ),
    "memory": (
        "Persistent memory index:\n"
        "{memories}\n\n"
        "The memory index is only a catalog. When full memories are relevant, they "
        "may be injected into the current turn inside <persistent_memories>. Treat "
        "those injected memories as durable user/project context, but prefer the "
        "latest explicit user message if there is a conflict."
    ),
}

_last_context_key: str | None = None
_last_prompt: str | None = None


def _format_tool_names(enabled_tools: list[str]) -> str:
    """Render enabled tool names as stable prompt text."""
    if not enabled_tools:
        return "- (no tools enabled)"
    return "\n".join(f"- {name}" for name in enabled_tools)


def _read_memory_context() -> str:
    """Load memory section content from real filesystem state."""
    if not MEMORY_INDEX.is_file():
        return ""

    content = MEMORY_INDEX.read_text(encoding="utf-8", errors="replace").strip()
    return content


def _task_context(enabled_tools: list[str]) -> dict:
    """Return task-system context only when task tools are actually enabled."""
    task_tools = {
        "create_task",
        "list_tasks",
        "get_task",
        "claim_task",
        "complete_task",
        "list_task_events",
    }
    enabled = task_tools.issubset(set(enabled_tools))
    if not enabled:
        return {"enabled": False, "task_count": 0}

    # The prompt gets a cheap count, not full task rows. Full details are
    # available through list_tasks/get_task/list_task_events when needed.
    try:
        from .database.autonomous_tasks import TASK_STORE

        task_count = TASK_STORE.task_count()
    except Exception:
        task_count = 0
    return {
        "enabled": True,
        "team_db": str(TEAM_DB),
        "task_count": task_count,
    }


def _active_team_count() -> int:
    """Read team state lazily so prompt assembly stays cheap and testable."""
    try:
        from .features.team import active_teammate_count

        return active_teammate_count()
    except Exception:
        # Prompt assembly should not fail just because optional team runtime
        # dependencies are unavailable in a smoke-test environment.
        return 0


def _pending_protocol_count() -> int:
    """Read protocol state lazily so prompt assembly stays import-safe."""
    if not TEAM_DB.exists():
        return 0
    try:
        from .database.team_protocols import PROTOCOLS

        return PROTOCOLS.pending_count()
    except Exception:
        return 0


def _worktree_context(enabled_tools: list[str]) -> dict:
    """Return worktree prompt context only when s18 tools are enabled."""
    worktree_tools = {
        "create_worktree",
        "bind_task_to_worktree",
        "list_worktrees",
        "keep_worktree",
        "remove_worktree",
        "list_worktree_events",
        "diff_worktree",
        "review_worktree",
        "test_worktree",
        "commit_worktree",
        "prepare_merge_worktree",
        "merge_worktree",
    }
    enabled = worktree_tools.issubset(set(enabled_tools))
    if not enabled:
        return {"enabled": False, "count": 0}
    try:
        from .database.worktrees import WORKTREE_STORE

        count = len(WORKTREE_STORE.list_worktrees())
    except Exception:
        count = 0
    return {"enabled": True, "count": count}


def update_context(messages: list, enabled_tools: list[str]) -> dict:
    """Build prompt context from runtime state, not message keyword guessing."""
    return {
        "workspace": str(WORKDIR),
        "project_root": str(PROJECT_ROOT),
        "state_dir": str(STATE_DIR),
        "enabled_tools": list(enabled_tools),
        "skills": list_skills(),
        "memories": _read_memory_context(),
        "tasks": _task_context(enabled_tools),
        "background": {
            "enabled": "bash" in enabled_tools,
            "running": running_background_count(),
        },
        "cron": {
            "enabled": {"schedule_cron", "list_crons", "cancel_cron"}.issubset(
                set(enabled_tools)
            ),
            "scheduled": scheduled_cron_count(),
            "queued": queued_cron_count(),
        },
        "team": {
            "enabled": {"spawn_teammate", "send_message", "check_inbox"}.issubset(
                set(enabled_tools)
            ),
            "db": str(TEAM_DB),
            "active": _active_team_count(),
        },
        "team_protocols": {
            "enabled": {
                "request_shutdown",
                "request_plan",
                "review_plan",
                "submit_plan",
            }.issubset(set(enabled_tools)),
            "pending": _pending_protocol_count(),
        },
        "worktrees": _worktree_context(enabled_tools),
    }


def assemble_system_prompt(context: dict) -> str:
    """Select sections based on context and join them into the final prompt."""
    sections = [
        ("identity", PROMPT_SECTIONS["identity"]),
        ("workspace", PROMPT_SECTIONS["workspace"].format(
            workspace=context.get("workspace", WORKDIR),
            project_root=context.get("project_root", PROJECT_ROOT),
            state_dir=context.get("state_dir", STATE_DIR),
        )),
        ("tools", PROMPT_SECTIONS["tools"].format(
            enabled_tools=_format_tool_names(context.get("enabled_tools", [])),
        )),
        ("skills", PROMPT_SECTIONS["skills"].format(
            skills=context.get("skills") or "- (no skills found)",
        )),
    ]

    # The task-system section is loaded from enabled tool state. This keeps the
    # prompt truthful if a future runner disables task tools.
    tasks = context.get("tasks") or {}
    if tasks.get("enabled"):
        sections.append(("tasks", PROMPT_SECTIONS["tasks"].format(
            team_db=tasks.get("team_db", TEAM_DB),
            task_count=tasks.get("task_count", 0),
        )))

    # Background execution is an execution policy for bash, so load it only when
    # bash is available in this runner.
    background = context.get("background") or {}
    if background.get("enabled"):
        sections.append(("background", PROMPT_SECTIONS["background"].format(
            running_background_tasks=background.get("running", 0),
        )))

    # Cron is loaded when its tools are enabled. It describes scheduled prompt
    # production, separate from background shell execution.
    cron = context.get("cron") or {}
    if cron.get("enabled"):
        sections.append(("cron", PROMPT_SECTIONS["cron"].format(
            scheduled_cron_jobs=cron.get("scheduled", 0),
            queued_cron_triggers=cron.get("queued", 0),
        )))

    # Team tools add persistent teammate communication through SQLite. Keep this
    # separate from subagent guidance because teammates are long-lived actors,
    # while subagents are one-shot context isolation helpers.
    team = context.get("team") or {}
    if team.get("enabled"):
        sections.append(("team", PROMPT_SECTIONS["team"].format(
            team_db=team.get("db", TEAM_DB),
            active_teammates=team.get("active", 0),
        )))

    team_protocols = context.get("team_protocols") or {}
    if team_protocols.get("enabled"):
        sections.append(("team_protocols", PROMPT_SECTIONS["team_protocols"].format(
            pending_protocol_requests=team_protocols.get("pending", 0),
        )))

    worktrees = context.get("worktrees") or {}
    if worktrees.get("enabled"):
        sections.append(("worktrees", PROMPT_SECTIONS["worktrees"].format(
            worktrees_dir=WORKTREES_DIR,
            worktree_count=worktrees.get("count", 0),
        )))

    # The memory section is loaded only when MEMORY.md exists and has content.
    # This keeps unrelated turns shorter and less noisy.
    memories = context.get("memories", "")
    if memories:
        sections.append(("memory", PROMPT_SECTIONS["memory"].format(memories=memories)))

    print(f"\033[90m[assembled] sections: {', '.join(name for name, _ in sections)}\033[0m")
    return "\n\n".join(section for _, section in sections)


def get_system_prompt(context: dict) -> str:
    """Return a cached prompt when the runtime context has not changed."""
    global _last_context_key, _last_prompt

    # json.dumps gives a deterministic key for nested dict/list values. Python's
    # hash() is process-randomized and cannot hash dicts directly.
    context_key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if context_key == _last_context_key and _last_prompt:
        print("\033[90m[cache hit] system prompt\033[0m")
        return _last_prompt

    _last_context_key = context_key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


def build_system() -> str:
    """Backward-compatible helper for callers that do not manage context yet."""
    return get_system_prompt(update_context(messages=[], enabled_tools=[]))


SUB_SYSTEM = (
    f"You are a focused subagent working at {WORKDIR}. Complete the delegated task "
    "directly with the available tools. Return a concise final summary with key findings, "
    "files changed, and any verification performed. Do not delegate further."
)
