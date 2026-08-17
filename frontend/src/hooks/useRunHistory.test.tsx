// @vitest-environment jsdom

import {act, renderHook, waitFor} from "@testing-library/react";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";

import {listRunHistory} from "../api/client";
import type {RunSnapshot} from "../types/runtime";
import {useRunHistory} from "./useRunHistory";


vi.mock("../api/client", () => ({listRunHistory: vi.fn()}));

const listRunHistoryMock = vi.mocked(listRunHistory);

function snapshot(id: string, status: RunSnapshot["status"] = "completed"): RunSnapshot {
  return {
    id,
    title: `Task ${id}`,
    status,
    messages: [],
    events: [],
    started_at: `2026-08-17T00:00:0${id.slice(-1)}Z`,
    completed_at: status === "completed" ? "2026-08-17T00:01:00Z" : null,
    error: null,
    last_sequence: 0,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return {promise, resolve};
}

beforeEach(() => {
  listRunHistoryMock.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useRunHistory", () => {
  it("loads the first page and uses the server cursor for Load more", async () => {
    listRunHistoryMock
      .mockResolvedValueOnce({items: [snapshot("run_2")], nextCursor: "cursor_2"})
      .mockResolvedValueOnce({items: [snapshot("run_1")], nextCursor: null});

    const {result} = renderHook(() => useRunHistory());

    await waitFor(() => expect(result.current.runs.map((run) => run.id)).toEqual(["run_2"]));
    expect(result.current.hasMore).toBe(true);

    await act(async () => {
      await result.current.loadMore();
    });

    expect(listRunHistoryMock).toHaveBeenLastCalledWith(expect.objectContaining({
      cursor: "cursor_2",
      limit: 25,
    }));
    expect(result.current.runs.map((run) => run.id)).toEqual(["run_2", "run_1"]);
    expect(result.current.hasMore).toBe(false);
  });

  it("clears stale rows, aborts the old request, and loads the new status filter", async () => {
    const firstRequest = deferred<{items: RunSnapshot[]; nextCursor: string | null}>();
    listRunHistoryMock
      .mockImplementationOnce(() => firstRequest.promise)
      .mockResolvedValueOnce({items: [snapshot("run_3")], nextCursor: null});

    const {result} = renderHook(() => useRunHistory());
    await waitFor(() => expect(listRunHistoryMock).toHaveBeenCalledTimes(1));
    const firstSignal = listRunHistoryMock.mock.calls[0]?.[0]?.signal;

    act(() => result.current.setFilter("completed"));
    expect(result.current.runs).toEqual([]);
    expect(firstSignal?.aborted).toBe(true);

    await waitFor(() => expect(result.current.runs.map((run) => run.id)).toEqual(["run_3"]));
    expect(listRunHistoryMock).toHaveBeenLastCalledWith(expect.objectContaining({
      status: "completed",
      cursor: null,
    }));

    // An aborted request may still resolve in a transport implementation. Its
    // request id must prevent it from overwriting the selected filter's page.
    firstRequest.resolve({items: [snapshot("run_old")], nextCursor: null});
    await act(async () => undefined);
    expect(result.current.runs.map((run) => run.id)).toEqual(["run_3"]);
  });
});
