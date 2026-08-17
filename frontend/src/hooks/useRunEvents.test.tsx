// @vitest-environment jsdom

import {act, renderHook} from "@testing-library/react";
import {beforeEach, describe, expect, it, vi} from "vitest";

import type {RunAction} from "../state/runReducer";
import {useRunEvents} from "./useRunEvents";


class MockEventSource {
  static instances: MockEventSource[] = [];

  readonly url: string;
  readonly listeners = new Map<string, Set<EventListener>>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string | URL) {
    this.url = String(url);
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  close() {
    this.closed = true;
  }

  emit(type: string, data = "") {
    const message = new MessageEvent(type, {data});
    for (const listener of this.listeners.get(type) ?? []) {
      listener(message);
    }
  }
}


beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
});


describe("useRunEvents", () => {
  it("connects with last sequence and dispatches named events", () => {
    const dispatch = vi.fn<(action: RunAction) => void>();
    const {result} = renderHook(() =>
      useRunEvents("run 1", "running", 7, dispatch),
    );
    const source = MockEventSource.instances[0];

    expect(source.url).toBe("/api/runs/run%201/events?after=7");
    act(() => source.onopen?.());
    expect(result.current.status).toBe("connected");

    act(() => source.emit("tool.started", JSON.stringify({
      id: "evt_8",
      run_id: "run 1",
      sequence: 8,
      schema_version: 1,
      type: "tool.started",
      created_at: "2026-08-17T00:00:08Z",
      payload: {tool_use_id: "toolu_1"},
    })));
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({
      type: "event.received",
    }));
  });

  it("closes the old connection on run switch and unmount", () => {
    const dispatch = vi.fn<(action: RunAction) => void>();
    const {rerender, unmount} = renderHook(
      ({runId}) => useRunEvents(runId, "running", 0, dispatch),
      {initialProps: {runId: "run_a" as string | null}},
    );
    const first = MockEventSource.instances[0];

    rerender({runId: "run_b"});
    const second = MockEventSource.instances[1];
    expect(first.closed).toBe(true);

    unmount();
    expect(second.closed).toBe(true);
  });

  it("keeps heartbeat out of dispatch and records malformed events", () => {
    const dispatch = vi.fn<(action: RunAction) => void>();
    const {result} = renderHook(() =>
      useRunEvents("run_1", "running", 0, dispatch),
    );
    const source = MockEventSource.instances[0];

    act(() => source.emit("heartbeat", "{}"));
    expect(result.current.status).toBe("connected");
    expect(dispatch).not.toHaveBeenCalled();

    act(() => source.emit("assistant.message", "not-json"));
    expect(result.current.error).toContain("malformed");
  });

  it("reconnects with the latest event cursor after a disconnect", () => {
    vi.useFakeTimers();
    const dispatch = vi.fn<(action: RunAction) => void>();
    renderHook(() => useRunEvents("run_1", "running", 0, dispatch));
    const first = MockEventSource.instances[0];

    act(() => first.emit("tool.started", JSON.stringify({
      id: "evt_3",
      run_id: "run_1",
      sequence: 3,
      schema_version: 1,
      type: "tool.started",
      created_at: "2026-08-17T00:00:03Z",
      payload: {tool_use_id: "toolu_1"},
    })));
    act(() => first.onerror?.());
    expect(first.closed).toBe(true);

    act(() => vi.advanceTimersByTime(500));
    expect(MockEventSource.instances[1].url).toBe("/api/runs/run_1/events?after=3");
    vi.useRealTimers();
  });

  it("marks the live connection offline after repeated retry failures", () => {
    vi.useFakeTimers();
    const dispatch = vi.fn<(action: RunAction) => void>();
    const {result, unmount} = renderHook(() =>
      useRunEvents("run_1", "running", 0, dispatch),
    );

    act(() => MockEventSource.instances[0].onerror?.());
    act(() => vi.advanceTimersByTime(500));
    act(() => MockEventSource.instances[1].onerror?.());
    act(() => vi.advanceTimersByTime(1_000));
    act(() => MockEventSource.instances[2].onerror?.());

    expect(result.current.status).toBe("offline");
    unmount();
    vi.useRealTimers();
  });

  it("does not connect without a run or after a terminal status", () => {
    const dispatch = vi.fn<(action: RunAction) => void>();
    const {rerender, result} = renderHook(
      ({runId, status}) => useRunEvents(runId, status, 0, dispatch),
      {
        initialProps: {
          runId: null as string | null,
          status: null as "completed" | null,
        },
      },
    );
    expect(MockEventSource.instances).toHaveLength(0);
    expect(result.current.status).toBe("idle");

    rerender({runId: "run_done", status: "completed"});
    expect(MockEventSource.instances).toHaveLength(0);
    expect(result.current.status).toBe("replay-complete");
  });
});
