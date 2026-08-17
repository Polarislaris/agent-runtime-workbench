import {describe, expect, it} from "vitest";

import type {RunEvent, RunSnapshot} from "../types/runtime";
import {
  createInitialRunState,
  hydrateRunSnapshot,
  runReducer,
} from "./runReducer";

function event(
  sequence: number,
  type: RunEvent["type"],
  payload: Record<string, unknown> = {},
): RunEvent {
  return {
    id: `evt_${sequence}`,
    run_id: "run_test",
    sequence,
    schema_version: 1,
    type,
    created_at: `2026-08-17T00:00:0${sequence}Z`,
    payload,
  };
}

function snapshot(events: RunEvent[] = []): RunSnapshot {
  return {
    id: "run_test",
    title: "Test run",
    status: "running",
    messages: [{role: "user", content: "hello"}],
    events,
    started_at: "2026-08-17T00:00:00Z",
    completed_at: null,
    error: null,
    last_sequence: events.length,
  };
}

describe("runReducer", () => {
  it("hydrates a snapshot without duplicating assistant history", () => {
    const source = snapshot([
      event(1, "run.started", {status: "running"}),
      event(2, "assistant.message", {text: "already in snapshot"}),
    ]);
    source.messages.push({role: "assistant", content: "already in snapshot"});

    const state = hydrateRunSnapshot(source);

    expect(state.messages).toHaveLength(2);
    expect(state.orderedEventIds).toEqual(["evt_1", "evt_2"]);
  });

  it("deduplicates by event id and processed sequence", () => {
    const started = event(1, "run.started");
    let state = runReducer(createInitialRunState(), {
      type: "event.received",
      event: started,
    });
    state = runReducer(state, {type: "event.received", event: started});
    state = runReducer(state, {
      type: "event.received",
      event: {...event(1, "model.started"), id: "evt_other"},
    });

    expect(state.orderedEventIds).toEqual(["evt_1"]);
  });

  it("hydrates in sequence order and keeps the snapshot cursor for SSE replay", () => {
    const source = snapshot([
      event(2, "assistant.message", {text: "second"}),
      event(1, "run.started", {status: "running"}),
    ]);
    source.last_sequence = 8;

    const state = runReducer(createInitialRunState(), {
      type: "snapshot.hydrated",
      snapshot: source,
    });

    expect(state.orderedEventIds).toEqual(["evt_1", "evt_2"]);
    expect(state.lastSequence).toBe(8);
  });

  it("updates one tool record from started to completed", () => {
    let state = hydrateRunSnapshot(snapshot());
    state = runReducer(state, {
      type: "event.received",
      event: event(1, "tool.started", {
        tool_use_id: "toolu_1",
        tool: "read_file",
        input_summary: {path: "README.md"},
        is_mock_mcp: false,
      }),
    });
    state = runReducer(state, {
      type: "event.received",
      event: event(2, "tool.completed", {
        tool_use_id: "toolu_1",
        tool: "read_file",
        duration_ms: 18,
        output_preview: "preview",
      }),
    });

    expect(state.toolsByUseId.toolu_1).toMatchObject({
      status: "completed",
      duration_ms: 18,
      output_preview: "preview",
      started_sequence: 1,
      completed_sequence: 2,
    });
  });

  it("updates one permission card and restores running status", () => {
    let state = hydrateRunSnapshot(snapshot());
    state = runReducer(state, {
      type: "event.received",
      event: event(1, "permission.requested", {
        request_id: "perm_1",
        tool: "bash",
        reason: "confirm",
        args_preview: {command: "rm file"},
      }),
    });
    expect(state.snapshot?.status).toBe("waiting_permission");

    state = runReducer(state, {
      type: "event.received",
      event: event(2, "permission.resolved", {
        request_id: "perm_1",
        tool: "bash",
        decision: "deny",
        resolution: "user",
      }),
    });

    expect(state.permissionsById.perm_1).toMatchObject({
      status: "resolved",
      decision: "deny",
      resolved_sequence: 2,
    });
    expect(state.snapshot?.status).toBe("running");
  });

  it("appends live assistant text and disables stop at terminal", () => {
    let state = hydrateRunSnapshot(snapshot());
    state = runReducer(state, {
      type: "event.received",
      event: event(1, "assistant.message", {text: "live answer"}),
    });
    state = runReducer(state, {
      type: "event.received",
      event: event(2, "run.completed", {status: "completed"}),
    });

    expect(state.messages.at(-1)).toEqual({
      role: "assistant",
      content: "live answer",
    });
    expect(state.snapshot?.status).toBe("completed");
    expect(state.canStop).toBe(false);
    expect(state.isComposerDisabled).toBe(false);
  });
});
