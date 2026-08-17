import type {RunEvent, RunSnapshot, WorkspaceTask} from "../types/runtime";


export interface LocalTodo {
  content: string;
  status: "pending" | "in_progress" | "completed";
}

export interface RunSummaryData {
  changedFiles: string[];
  checks: Array<{label: string; status: "passed" | "failed" | "unknown"}>;
  finalStatus: RunSnapshot["status"];
  error: string | null;
  elapsedMs: number;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

/**
 * todo_write is intentionally a per-run working checklist, not a database
 * task. The newest successful tool input is the complete current checklist.
 */
export function latestLocalTodos(events: RunEvent[]): LocalTodo[] {
  const latest = [...events].reverse().find((event) =>
    event.type === "tool.started" && event.payload.tool === "todo_write",
  );
  const input = record(latest?.payload.input_summary);
  const values = input?.todos;
  if (!Array.isArray(values)) return [];

  return values.flatMap((value) => {
    const todo = record(value);
    const content = typeof todo?.content === "string" ? todo.content.trim() : "";
    const status = todo?.status;
    if (!content || !["pending", "in_progress", "completed"].includes(String(status))) {
      return [];
    }
    return [{content, status: status as LocalTodo["status"]}];
  });
}

/** Extract stable worktree names without treating event text as an identifier. */
export function relatedWorktreeNames(events: RunEvent[], tasks: WorkspaceTask[] = []): string[] {
  const names = new Set<string>();
  for (const task of tasks) {
    if (task.worktree_name) names.add(task.worktree_name);
  }
  for (const event of events) {
    const name = event.payload.worktree_name;
    if (typeof name === "string" && name) names.add(name);
  }
  return [...names].sort();
}

function changedFileFromTool(event: RunEvent): string | null {
  if (event.type !== "tool.started") return null;
  const tool = event.payload.tool;
  if (tool !== "write_file" && tool !== "edit_file") return null;
  const input = record(event.payload.input_summary);
  const path = input?.path;
  return typeof path === "string" && path ? path : null;
}

/**
 * Build a compact read model only from the durable snapshot and event stream.
 * No summary field is persisted in the browser, so SSE replay and hydration
 * always produce the same Inspector result.
 */
export function deriveRunSummary(snapshot: RunSnapshot, events: RunEvent[]): RunSummaryData {
  const changedFiles = [...new Set(events.flatMap((event) => {
    const path = changedFileFromTool(event);
    return path ? [path] : [];
  }))];
  const checks = events.flatMap((event) => {
    if (event.type !== "worktree.checked") return [];
    const command = typeof event.payload.command === "string"
      ? event.payload.command
      : "Worktree check";
    const status = event.payload.status;
    const checkStatus: "passed" | "failed" | "unknown" =
      status === "passed" || status === "failed" ? status : "unknown";
    return [{
      label: command,
      status: checkStatus,
    }];
  });
  const started = new Date(snapshot.started_at).getTime();
  const ended = new Date(snapshot.completed_at ?? Date.now()).getTime();

  return {
    changedFiles,
    checks,
    finalStatus: snapshot.status,
    error: snapshot.error,
    elapsedMs: Number.isNaN(started) || Number.isNaN(ended) ? 0 : Math.max(0, ended - started),
  };
}
