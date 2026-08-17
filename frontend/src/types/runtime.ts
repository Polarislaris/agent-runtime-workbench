export type RunStatus =
  | "queued"
  | "running"
  | "waiting_permission"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type RunEventType =
  | "run.started"
  | "model.started"
  | "model.completed"
  | "assistant.message"
  | "tool.started"
  | "tool.completed"
  | "tool.failed"
  | "permission.requested"
  | "permission.resolved"
  | "run.completed"
  | "run.failed"
  | "run.cancelled"
  | "run.interrupted"
  | "task.created"
  | "task.claimed"
  | "task.completed"
  | "task.failed"
  | "agent.spawned"
  | "agent.status"
  | "agent.completed"
  | "team.message"
  | "worktree.created"
  | "worktree.bound"
  | "worktree.diffed"
  | "worktree.reviewed"
  | "worktree.checked"
  | "worktree.committed"
  | "worktree.merge_prepared"
  | "worktree.merged"
  | "worktree.kept"
  | "worktree.removed"
  | "worktree.failed"
  | "retry.scheduled"
  | "context.compacted"
  | "background.started"
  | "background.completed"
  | "cron.fired";

export type PermissionDecision = "allow" | "deny";
export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export type JsonObject = { [key: string]: JsonValue };

export interface RunEvent<TPayload extends Record<string, unknown> = Record<string, unknown>> {
  id: string;
  run_id: string;
  sequence: number;
  schema_version: number;
  type: RunEventType;
  created_at: string;
  payload: TPayload;
}

export interface MessageContentBlock {
  type: string;
  id?: string;
  name?: string;
  input?: JsonObject;
  text?: string;
  content?: string;
  tool_use_id?: string;
  is_error?: boolean;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string | MessageContentBlock[];
}

export interface RunSnapshot {
  id: string;
  title: string;
  status: RunStatus;
  messages: ChatMessage[];
  events: RunEvent[];
  started_at: string;
  completed_at: string | null;
  error: string | null;
  last_sequence: number;
}

export interface ToolExecution {
  tool_use_id: string;
  tool: string;
  status: "running" | "completed" | "failed";
  input_summary: JsonObject;
  output_preview?: string;
  error?: string;
  duration_ms?: number;
  is_mock_mcp: boolean;
  started_sequence?: number;
  completed_sequence?: number;
}

export interface PermissionRequest {
  request_id: string;
  tool: string;
  reason: string;
  args_preview: JsonObject;
  status: "pending" | "resolved";
  decision?: PermissionDecision;
  resolution?: "user" | "timeout" | "cancelled" | "run_terminated";
  requested_sequence?: number;
  resolved_sequence?: number;
}

/** Durable SQLite task-board row returned by the Inspector API. */
export interface WorkspaceTask {
  id: string;
  task_id: string;
  subject: string;
  description: string;
  status: "pending" | "in_progress" | "completed" | "failed" | "cancelled";
  owner: string | null;
  worktree_name: string | null;
  worktree: string | null;
  priority: number;
  error: string | null;
  blockedBy: string[];
}

/** Durable teammate lifecycle row returned by the Inspector API. */
export interface WorkspaceAgent {
  agent_id: string;
  role: string;
  status: "running" | "idle" | "shutting_down" | "done" | "failed";
  current_task_id: string | null;
  error: string | null;
}

/** Read-only worktree diff snapshot, loaded only after the drawer opens. */
export interface WorktreeDiff {
  worktree_name: string;
  task_id: string | null;
  status: string;
  path: string;
  branch: string;
  git_status_short: string;
  git_diff_stat: string;
  git_diff_name_only: string;
  git_diff?: string;
}

/** Persisted test/check result; the UI never executes this command itself. */
export interface WorktreeCheck {
  check_id: number;
  worktree_name: string;
  task_id: string | null;
  command: string;
  exit_code: number;
  output_preview: string;
  status: "passed" | "failed";
  created_at: number;
}
