import type {
  PermissionDecision,
  RunSnapshot,
  RunStatus,
  WorkspaceAgent,
  WorkspaceTask,
  WorktreeCheck,
  WorktreeDiff,
} from "../types/runtime";

export interface RunHistoryPage {
  items: RunSnapshot[];
  nextCursor: string | null;
}

export interface ListRunHistoryOptions {
  status?: RunStatus;
  cursor?: string | null;
  limit?: number;
  signal?: AbortSignal;
}

interface WorkspaceItems<T> {
  scope: "workspace";
  items: T[];
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const {body} = await requestWithResponse<T>(path, init);
  return body;
}

async function requestWithResponse<T>(
  path: string,
  init?: RequestInit,
): Promise<{body: T; response: Response}> {
  const headers = new Headers(init?.headers);
  if (init?.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`/api${path}`, {...init, headers});
  const contentType = response.headers.get("content-type") ?? "";
  let body: unknown = null;

  if (response.status !== 204) {
    body = contentType.includes("application/json")
      ? await response.json()
      : await response.text();
  }

  if (!response.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? (body as {detail: unknown}).detail
        : body;
    const message =
      typeof detail === "string"
        ? detail
        : `API request failed with status ${response.status}`;
    throw new ApiError(response.status, message, body);
  }

  if (response.status === 204) {
    return {body: undefined as T, response};
  }
  return {body: body as T, response};
}

export function createRun(prompt: string): Promise<RunSnapshot> {
  return request<RunSnapshot>("/runs", {
    method: "POST",
    body: JSON.stringify({prompt}),
  });
}

export function listRuns(): Promise<RunSnapshot[]> {
  return request<RunSnapshot[]>("/runs");
}

export async function listRunHistory(
  options: ListRunHistoryOptions = {},
): Promise<RunHistoryPage> {
  const query = new URLSearchParams();
  if (options.status) query.set("status", options.status);
  if (options.cursor) query.set("cursor", options.cursor);
  if (options.limit) query.set("limit", String(options.limit));
  const suffix = query.size ? `?${query.toString()}` : "";
  const {body, response} = await requestWithResponse<RunSnapshot[]>(
    `/runs${suffix}`,
    {signal: options.signal},
  );
  return {
    items: body,
    nextCursor: response.headers.get("X-Next-Cursor"),
  };
}

export function getRun(runId: string, signal?: AbortSignal): Promise<RunSnapshot> {
  return request<RunSnapshot>(`/runs/${encodeURIComponent(runId)}`, {signal});
}

export function cancelRun(runId: string): Promise<RunSnapshot> {
  return request<RunSnapshot>(
    `/runs/${encodeURIComponent(runId)}/cancel`,
    {method: "POST"},
  );
}

export function resolvePermission(
  runId: string,
  requestId: string,
  decision: PermissionDecision,
): Promise<void> {
  return request<void>(
    `/runs/${encodeURIComponent(runId)}/permissions/${encodeURIComponent(requestId)}`,
    {
      method: "POST",
      body: JSON.stringify({decision}),
    },
  );
}

/**
 * Task and agent rows are workspace-scoped today. The selected run ID still
 * matters: the server validates it before returning any project-level data.
 */
export function getRunTasks(runId: string, signal?: AbortSignal): Promise<WorkspaceItems<WorkspaceTask>> {
  return request<WorkspaceItems<WorkspaceTask>>(
    `/runs/${encodeURIComponent(runId)}/tasks`,
    {signal},
  );
}

export function getRunAgents(runId: string, signal?: AbortSignal): Promise<WorkspaceItems<WorkspaceAgent>> {
  return request<WorkspaceItems<WorkspaceAgent>>(
    `/runs/${encodeURIComponent(runId)}/agents`,
    {signal},
  );
}

/** Read a patch on demand. Opening a browser drawer must not alter Git state. */
export function getWorktreeDiff(name: string, signal?: AbortSignal): Promise<WorktreeDiff> {
  return request<WorktreeDiff>(`/worktrees/${encodeURIComponent(name)}/diff`, {signal});
}

export function getWorktreeChecks(
  name: string,
  signal?: AbortSignal,
): Promise<{items: WorktreeCheck[]}> {
  return request<{items: WorktreeCheck[]}>(
    `/worktrees/${encodeURIComponent(name)}/checks`,
    {signal},
  );
}
