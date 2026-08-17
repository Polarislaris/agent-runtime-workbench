import type {
  ChatMessage,
  JsonObject,
  PermissionDecision,
  PermissionRequest,
  RunEvent,
  RunSnapshot,
  RunStatus,
  ToolExecution,
} from "../types/runtime";

export interface RunViewState {
  snapshot: RunSnapshot | null;
  eventsById: Record<string, RunEvent>;
  orderedEventIds: string[];
  messages: ChatMessage[];
  toolsByUseId: Record<string, ToolExecution>;
  permissionsById: Record<string, PermissionRequest>;
  lastSequence: number;
  isComposerDisabled: boolean;
  canStop: boolean;
}

export type RunAction =
  | {type: "snapshot.loaded"; snapshot: RunSnapshot}
  | {type: "snapshot.hydrated"; snapshot: RunSnapshot}
  | {type: "event.received"; event: RunEvent}
  | {type: "reset"};

const TERMINAL_STATUSES: RunStatus[] = ["completed", "failed", "cancelled", "interrupted"];

function controlsForStatus(status: RunStatus | null) {
  const isTerminal = status !== null && TERMINAL_STATUSES.includes(status);
  return {
    isComposerDisabled: status === null || !isTerminal,
    canStop: status !== null && !isTerminal,
  };
}

export function createInitialRunState(): RunViewState {
  return {
    snapshot: null,
    eventsById: {},
    orderedEventIds: [],
    messages: [],
    toolsByUseId: {},
    permissionsById: {},
    lastSequence: 0,
    isComposerDisabled: true,
    canStop: false,
  };
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNumber(value: unknown): number | undefined {
  return typeof value === "number" ? value : undefined;
}

function asObject(value: unknown): JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

function updateStatus(state: RunViewState, status: RunStatus): RunViewState {
  const controls = controlsForStatus(status);
  return {
    ...state,
    snapshot: state.snapshot ? {...state.snapshot, status} : null,
    ...controls,
  };
}

function applyEvent(
  state: RunViewState,
  event: RunEvent,
  appendAssistantMessage: boolean,
): RunViewState {
  if (
    state.eventsById[event.id]
    || state.orderedEventIds.some((id) => state.eventsById[id].sequence === event.sequence)
  ) {
    return state;
  }

  const eventsById = {...state.eventsById, [event.id]: event};
  const orderedEventIds = [...state.orderedEventIds, event.id].sort(
    (left, right) => eventsById[left].sequence - eventsById[right].sequence,
  );
  let next: RunViewState = {
    ...state,
    eventsById,
    orderedEventIds,
    // A catch-up stream can deliver sequence 3 before sequence 2. Preserve the
    // high-water cursor for reconnects while keeping both events in UI order.
    lastSequence: Math.max(state.lastSequence, event.sequence),
  };
  const payload = event.payload;

  if (event.type === "assistant.message" && appendAssistantMessage) {
    const text = asString(payload.text);
    if (text) {
      next = {
        ...next,
        messages: [...next.messages, {role: "assistant", content: text}],
      };
    }
  }

  if (event.type === "tool.started") {
    const toolUseId = asString(payload.tool_use_id);
    if (toolUseId) {
      next = {
        ...next,
        toolsByUseId: {
          ...next.toolsByUseId,
          [toolUseId]: {
            tool_use_id: toolUseId,
            tool: asString(payload.tool, "unknown"),
            status: "running",
            input_summary: asObject(payload.input_summary),
            is_mock_mcp: payload.is_mock_mcp === true,
            started_sequence: event.sequence,
          },
        },
      };
    }
  }

  if (event.type === "tool.completed" || event.type === "tool.failed") {
    const toolUseId = asString(payload.tool_use_id);
    if (toolUseId) {
      const previous = next.toolsByUseId[toolUseId];
      next = {
        ...next,
        toolsByUseId: {
          ...next.toolsByUseId,
          [toolUseId]: {
            tool_use_id: toolUseId,
            tool: asString(payload.tool, previous?.tool ?? "unknown"),
            status: event.type === "tool.completed" ? "completed" : "failed",
            input_summary: asObject(
              payload.input_summary ?? previous?.input_summary,
            ),
            output_preview: asString(payload.output_preview) || undefined,
            error: asString(payload.error) || undefined,
            duration_ms: asNumber(payload.duration_ms),
            is_mock_mcp:
              payload.is_mock_mcp === true || previous?.is_mock_mcp === true,
            started_sequence: previous?.started_sequence,
            completed_sequence: event.sequence,
          },
        },
      };
    }
  }

  if (event.type === "permission.requested") {
    const requestId = asString(payload.request_id);
    if (requestId) {
      next = {
        ...next,
        permissionsById: {
          ...next.permissionsById,
          [requestId]: {
            request_id: requestId,
            tool: asString(payload.tool, "unknown"),
            reason: asString(payload.reason),
            args_preview: asObject(payload.args_preview),
            status: "pending",
            requested_sequence: event.sequence,
          },
        },
      };
      next = updateStatus(next, "waiting_permission");
    }
  }

  if (event.type === "permission.resolved") {
    const requestId = asString(payload.request_id);
    if (requestId) {
      const previous = next.permissionsById[requestId];
      next = {
        ...next,
        permissionsById: {
          ...next.permissionsById,
          [requestId]: {
            request_id: requestId,
            tool: asString(payload.tool, previous?.tool ?? "unknown"),
            reason: previous?.reason ?? "",
            args_preview: previous?.args_preview ?? {},
            status: "resolved",
            decision: asString(payload.decision) as PermissionDecision,
            resolution: asString(payload.resolution) as PermissionRequest["resolution"],
            requested_sequence: previous?.requested_sequence,
            resolved_sequence: event.sequence,
          },
        },
      };
      next = updateStatus(next, "running");
    }
  }

  const statusByEvent: Partial<Record<RunEvent["type"], RunStatus>> = {
    "run.started": "running",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
    "run.interrupted": "interrupted",
  };
  const latestStatusEvent = orderedEventIds
    .map((id) => eventsById[id])
    .filter((candidate) => statusByEvent[candidate.type])
    .at(-1);
  const nextStatus = latestStatusEvent ? statusByEvent[latestStatusEvent.type] : undefined;
  if (nextStatus && latestStatusEvent) {
    next = updateStatus(next, nextStatus);
    if (next.snapshot && TERMINAL_STATUSES.includes(nextStatus)) {
      next = {
        ...next,
        snapshot: {
          ...next.snapshot,
          completed_at: latestStatusEvent.created_at,
          error:
            nextStatus === "failed"
              ? asString(latestStatusEvent.payload.error) || next.snapshot.error
              : next.snapshot.error,
        },
      };
    }
  }

  return next;
}

export function hydrateRunSnapshot(snapshot: RunSnapshot): RunViewState {
  const controls = controlsForStatus(snapshot.status);
  let state: RunViewState = {
    ...createInitialRunState(),
    snapshot,
    messages: [...snapshot.messages],
    ...controls,
  };

  for (const event of [...snapshot.events].sort(
    (left, right) => left.sequence - right.sequence,
  )) {
    state = applyEvent(state, event, false);
  }
  return {
    ...state,
    // The backend snapshot is authoritative at the instant it was read. This
    // cursor tells useRunEvents exactly where durable SSE catch-up should begin.
    lastSequence: Math.max(state.lastSequence, snapshot.last_sequence),
  };
}

export function runReducer(
  state: RunViewState,
  action: RunAction,
): RunViewState {
  switch (action.type) {
    case "snapshot.loaded":
    case "snapshot.hydrated":
      return hydrateRunSnapshot(action.snapshot);
    case "event.received":
      return applyEvent(state, action.event, true);
    case "reset":
      return createInitialRunState();
  }
}
