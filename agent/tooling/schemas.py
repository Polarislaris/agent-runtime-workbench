"""Tool schemas exposed to the model."""

from __future__ import annotations


# Builtin schemas stay static. ``assemble_tool_pool`` adds connected mock MCP
# tools at request time, because ``connect_mcp`` changes what the model can use.
BUILTIN_TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in the current workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run from the workspace root.",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Run a slow, independent command asynchronously. Use only when "
                        "immediate follow-up steps do not depend on this command finishing."
                    ),
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file from the current workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file, relative to the workspace root.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional maximum number of lines to return.",
                    "minimum": 1,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": "Write text content to a file in the current workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to write, relative to the workspace root.",
                },
                "content": {
                    "type": "string",
                    "description": "Complete file content to write.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_file",
        "description": "Replace the first exact occurrence of text in a workspace file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to edit, relative to the workspace root.",
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to replace.",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text.",
                },
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "glob",
        "description": "Find workspace files and directories matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Relative glob pattern, for example '*.py' or '**/*.md'.",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "todo_write",
        "description": (
            "Create and update a structured task list for multi-step work. "
            "Use this before starting complex tasks and update it as progress changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The complete current task list.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "A concrete task to complete.",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "Current task status.",
                            },
                        },
                        "required": ["content", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["todos"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task",
        "description": (
            "Launch a subagent to handle a focused subtask with fresh context. "
            "Use it for investigation, broad file review, or self-contained work where "
            "only the final conclusion should return to the parent conversation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A complete, self-contained task for the subagent.",
                },
            },
            "required": ["description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "load_skill",
        "description": (
            "Load the full instructions for an available skill by name. "
            "Use this when the user's request matches a skill listed in the system prompt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The exact skill name from the skills catalog.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_task",
        "description": (
            "Create a persistent project task in the SQLite task board. Use blockedBy "
            "to encode dependencies that must be completed before this task can be claimed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "Short task title.",
                },
                "description": {
                    "type": "string",
                    "description": "Detailed task instructions or acceptance criteria.",
                },
                "blockedBy": {
                    "type": "array",
                    "description": "Task IDs that must be completed before this task can start.",
                    "items": {"type": "string"},
                },
                "priority": {
                    "type": "integer",
                    "description": "Optional priority; higher priority tasks are scanned first.",
                },
            },
            "required": ["subject"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_tasks",
        "description": "List persistent project tasks from the SQLite task board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "failed", "cancelled"],
                    "description": "Optional status filter.",
                },
                "owner": {
                    "type": "string",
                    "description": "Optional owner filter, for example lead or alice.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_task",
        "description": "Read the full JSON details for one persistent task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task id, for example task_1720000000_a1b2.",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "claim_task",
        "description": (
            "Atomically claim a pending task for an owner. The SQLite transaction "
            "rejects claims when the task is owned, not pending, or blockedBy dependencies remain."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task id to claim.",
                },
                "owner": {
                    "type": "string",
                    "description": "Agent or user name that owns the in-progress task.",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "complete_task",
        "description": (
            "Mark an in-progress task completed in the SQLite task board and report "
            "downstream tasks that became unblocked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task id to complete.",
                },
                "owner": {
                    "type": "string",
                    "description": "Optional owner assertion. Teammates automatically complete only their own tasks.",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_task_events",
        "description": "List recent SQLite task lifecycle audit events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Optional task id filter.",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Optional agent id filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum events to return.",
                    "minimum": 1,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "create_worktree",
        "description": (
            "Create an isolated Git worktree under WORKDIR/.worktrees and optionally "
            "bind it to a pending task. Requires WORKDIR to be a local git repository."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Safe worktree name using letters, digits, dot, underscore, or dash.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Optional pending task id to bind without claiming it.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bind_task_to_worktree",
        "description": (
            "Bind an existing active worktree to a pending task. This does not claim "
            "the task; teammates claim it later and then run file tools inside the worktree."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pending task id to bind.",
                },
                "worktree_name": {
                    "type": "string",
                    "description": "Existing worktree name.",
                },
            },
            "required": ["task_id", "worktree_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_worktrees",
        "description": "List known Git worktrees and their SQLite lifecycle status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "active",
                        "ready_for_review",
                        "needs_changes",
                        "approved",
                        "committed",
                        "merged",
                        "kept",
                        "removed",
                        "failed",
                    ],
                    "description": "Optional worktree status filter.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "keep_worktree",
        "description": "Mark a worktree as kept for human review without deleting files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Worktree name to keep.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "remove_worktree",
        "description": (
            "Remove a Git worktree and delete its branch. Refuses worktrees with "
            "uncommitted changes unless discard_changes is true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Worktree name to remove.",
                },
                "discard_changes": {
                    "type": "boolean",
                    "description": "True to discard uncommitted changes while removing.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_worktree_events",
        "description": "List recent worktree lifecycle audit events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "worktree_name": {
                    "type": "string",
                    "description": "Optional worktree name filter.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Optional task id filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum events to return.",
                    "minimum": 1,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "diff_worktree",
        "description": (
            "Inspect changes in an isolated worktree without mutating Git state. "
            "Records a diffed audit event for the Lead review trail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Worktree name to inspect.",
                },
                "include_patch": {
                    "type": "boolean",
                    "description": "True to include git diff patch text, bounded by max_chars.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum patch characters to include when include_patch is true.",
                    "minimum": 200,
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "review_worktree",
        "description": (
            "Record a Lead review decision. approve=true marks the worktree approved; "
            "approve=false marks it needs_changes so the teammate can continue work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Worktree name to review.",
                },
                "approve": {
                    "type": "boolean",
                    "description": "True to approve, false to request more changes.",
                },
                "summary": {
                    "type": "string",
                    "description": "Short review conclusion.",
                },
                "notes": {
                    "type": "string",
                    "description": "Detailed review notes or requested changes.",
                },
            },
            "required": ["name", "approve"],
            "additionalProperties": False,
        },
    },
    {
        "name": "test_worktree",
        "description": (
            "Run a verification command inside an isolated worktree and record "
            "the result in SQLite."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Worktree name to test.",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to run from the worktree root.",
                },
            },
            "required": ["name", "command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "commit_worktree",
        "description": (
            "Create a Git commit from ready_for_review or approved worktree changes "
            "and record the commit sha."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Worktree name to commit.",
                },
                "message": {
                    "type": "string",
                    "description": "Non-empty Git commit message.",
                },
            },
            "required": ["name", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "prepare_merge_worktree",
        "description": (
            "Validate a merge target and persist a merge plan without changing branches "
            "or merging code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Approved or committed worktree name.",
                },
                "target_branch": {
                    "type": "string",
                    "description": "Branch to merge into. Defaults to main.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "merge_worktree",
        "description": (
            "Merge an approved or committed worktree branch into a target branch. "
            "This mutates the business project and requires explicit user confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Approved or committed worktree name.",
                },
                "target_branch": {
                    "type": "string",
                    "description": "Branch to merge into. Defaults to main.",
                },
                "user_confirmed": {
                    "type": "boolean",
                    "description": "Must be true only after explicit user confirmation.",
                },
            },
            "required": ["name", "user_confirmed"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_worktree_reviews",
        "description": "List persisted worktree review records.",
        "input_schema": {
            "type": "object",
            "properties": {
                "worktree_name": {
                    "type": "string",
                    "description": "Optional worktree name filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum records to return.",
                    "minimum": 1,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_worktree_checks",
        "description": "List persisted worktree test/check records.",
        "input_schema": {
            "type": "object",
            "properties": {
                "worktree_name": {
                    "type": "string",
                    "description": "Optional worktree name filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum records to return.",
                    "minimum": 1,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_worktree_merges",
        "description": "List persisted worktree merge plans and results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "worktree_name": {
                    "type": "string",
                    "description": "Optional worktree name filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum records to return.",
                    "minimum": 1,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "schedule_cron",
        "description": (
            "Schedule a prompt to be delivered to the agent by a five-field cron "
            "expression. The scheduler only produces work; agent_loop decides how to execute it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cron": {
                    "type": "string",
                    "description": "Five-field cron expression: minute hour day month weekday.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Prompt injected when the cron schedule fires.",
                },
                "recurring": {
                    "type": "boolean",
                    "description": "True for recurring jobs, false for one-shot jobs.",
                },
                "durable": {
                    "type": "boolean",
                    "description": "Persist this cron definition to .scheduled_tasks.json.",
                },
            },
            "required": ["cron", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_crons",
        "description": "List all scheduled cron jobs.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_cron",
        "description": "Cancel a scheduled cron job by id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Cron job id, for example cron_1720000000_a1b2.",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "spawn_teammate",
        "description": (
            "Create a named teammate agent for an independent workstream. "
            "Use this for larger tasks that benefit from separate context and async communication."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Stable teammate id, using letters, digits, underscore, or dash.",
                },
                "role": {
                    "type": "string",
                    "description": "Short role description, for example backend_dev or reviewer.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Complete initial task instructions for this teammate.",
                },
            },
            "required": ["name", "role", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send_message",
        "description": (
            "Send a durable SQLite inbox message to Lead or a teammate. "
            "Use this for follow-up instructions, progress updates, results, or protocol messages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to_agent": {
                    "type": "string",
                    "description": "Recipient agent id, for example lead or alice.",
                },
                "content": {
                    "type": "string",
                    "description": "Message body.",
                },
                "msg_type": {
                    "type": "string",
                    "description": "Optional message type such as message, result, permission_request, or shutdown_request.",
                },
            },
            "required": ["to_agent", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "check_inbox",
        "description": (
            "Read and consume pending SQLite inbox messages. Lead normally reads "
            "lead; teammates may only read their own inbox."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Agent id to check. Defaults to lead for the main Agent.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_teammates",
        "description": "List currently known teammate threads and their lifecycle status.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "request_shutdown",
        "description": (
            "Ask a teammate to shut down gracefully using a tracked request/response protocol."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Teammate id to shut down.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional shutdown reason or cleanup instruction.",
                },
            },
            "required": ["agent"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_plan",
        "description": (
            "Ask a teammate to submit a plan before risky work. The teammate should "
            "respond by calling submit_plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Teammate id that should submit a plan.",
                },
                "instruction": {
                    "type": "string",
                    "description": "Work or risk area that needs a plan before execution.",
                },
            },
            "required": ["agent", "instruction"],
            "additionalProperties": False,
        },
    },
    {
        "name": "review_plan",
        "description": (
            "Approve or reject a pending plan_approval request and notify the teammate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "Protocol request id from submit_plan.",
                },
                "approve": {
                    "type": "boolean",
                    "description": "True to approve the plan, false to reject it.",
                },
                "reason": {
                    "type": "string",
                    "description": "Approval note or rejection reason.",
                },
            },
            "required": ["request_id", "approve"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_protocol_requests",
        "description": "List request/response protocol states, optionally filtered by status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "approved", "rejected", "expired", "failed"],
                    "description": "Optional protocol status filter.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_plan",
        "description": (
            "Submit a plan_approval request to Lead. This is mainly for teammates "
            "before high-risk edits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "Detailed plan, including risk and rollback notes when relevant.",
                },
            },
            "required": ["plan"],
            "additionalProperties": False,
        },
    },
    {
        "name": "connect_mcp",
        "description": (
            "Connect to one of the preconfigured mock MCP servers and discover its tools. "
            "This s19 teaching implementation is in-process only; it does not contact a "
            "real MCP server. Available servers: docs, deploy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Mock MCP server to connect: docs or deploy.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "compact",
        "description": (
            "Compact the current conversation history into a concise working summary. "
            "Use this when context is getting long or the user asks to compact."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]

# Compatibility export for focused subagents and older chapters. It contains
# only builtin tools; s19's dynamic mock MCP tools are Lead-only by design.
TOOLS = BUILTIN_TOOLS
