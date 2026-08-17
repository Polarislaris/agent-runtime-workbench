// @vitest-environment jsdom

import {cleanup, fireEvent, render, screen, waitFor} from "@testing-library/react";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";

import type {RunSnapshot} from "./types/runtime";


const apiMocks = vi.hoisted(() => ({
  cancelRun: vi.fn(),
  createRun: vi.fn(),
  getRun: vi.fn(),
  resolvePermission: vi.fn(),
}));
const historyMocks = vi.hoisted(() => ({useRunHistory: vi.fn()}));

vi.mock("./api/client", () => apiMocks);
vi.mock("./hooks/useRunHistory", () => historyMocks);
vi.mock("./hooks/useRunEvents", () => ({
  useRunEvents: () => ({status: "idle", error: null}),
}));

import {App} from "./App";


function snapshot(overrides: Partial<RunSnapshot> = {}): RunSnapshot {
  return {
    id: "run_test",
    title: "Inspect login tests",
    status: "completed",
    messages: [{role: "user", content: "Inspect login tests"}],
    events: [],
    started_at: "2026-08-17T00:00:00Z",
    completed_at: "2026-08-17T00:00:02Z",
    error: null,
    last_sequence: 0,
    ...overrides,
  };
}

function historyState(overrides: Partial<{
  runs: RunSnapshot[];
  filter: "all" | RunSnapshot["status"];
  isLoading: boolean;
  isLoadingMore: boolean;
  error: string | null;
  hasMore: boolean;
}> = {}) {
  return {
    runs: [],
    filter: "all" as const,
    isLoading: false,
    isLoadingMore: false,
    error: null,
    hasMore: false,
    setFilter: vi.fn(),
    loadMore: vi.fn().mockResolvedValue(undefined),
    reload: vi.fn().mockResolvedValue(undefined),
    upsertRun: vi.fn(),
    ...overrides,
  };
}


beforeEach(() => {
  for (const mock of Object.values(apiMocks)) mock.mockReset();
  historyMocks.useRunHistory.mockReturnValue(historyState());
});

afterEach(() => cleanup());


describe("Agent Runtime Workbench", () => {
  it("renders the three-column empty state and creates a run", async () => {
    const created = snapshot({status: "running", completed_at: null});
    apiMocks.createRun.mockResolvedValue(created);

    render(<App />);
    expect(await screen.findByText("No runs yet.")).toBeTruthy();
    expect(screen.getByText("See the work, not just the answer.")).toBeTruthy();
    expect(screen.getByText("Execution details")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Agent task"), {
      target: {value: "Inspect login tests"},
    });
    fireEvent.click(screen.getByRole("button", {name: "Send"}));

    await waitFor(() => expect(apiMocks.createRun).toHaveBeenCalledWith(
      "Inspect login tests",
    ));
    expect((await screen.findAllByText("Inspect login tests")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", {name: "Stop"})).toBeTruthy();
  });

  it("hydrates tool, permission, assistant, and inspector views", async () => {
    const populated = snapshot({
      events: [
        {
          id: "evt_1",
          run_id: "run_test",
          sequence: 1,
          schema_version: 1,
          type: "run.started",
          created_at: "2026-08-17T00:00:00Z",
          payload: {status: "running"},
        },
        {
          id: "evt_2",
          run_id: "run_test",
          sequence: 2,
          schema_version: 1,
          type: "assistant.message",
          created_at: "2026-08-17T00:00:01Z",
          payload: {text: "I will inspect the failing test."},
        },
        {
          id: "evt_3",
          run_id: "run_test",
          sequence: 3,
          schema_version: 1,
          type: "tool.started",
          created_at: "2026-08-17T00:00:01Z",
          payload: {
            tool_use_id: "toolu_1",
            tool: "read_file",
            input_summary: {path: "test_login.py"},
            is_mock_mcp: false,
          },
        },
        {
          id: "evt_4",
          run_id: "run_test",
          sequence: 4,
          schema_version: 1,
          type: "tool.completed",
          created_at: "2026-08-17T00:00:01Z",
          payload: {
            tool_use_id: "toolu_1",
            tool: "read_file",
            duration_ms: 18,
            output_preview: "test contents",
          },
        },
        {
          id: "evt_5",
          run_id: "run_test",
          sequence: 5,
          schema_version: 1,
          type: "permission.requested",
          created_at: "2026-08-17T00:00:01Z",
          payload: {
            request_id: "perm_1",
            tool: "edit_file",
            reason: "Removing file content",
            args_preview: {path: "auth.py"},
          },
        },
        {
          id: "evt_6",
          run_id: "run_test",
          sequence: 6,
          schema_version: 1,
          type: "permission.resolved",
          created_at: "2026-08-17T00:00:01Z",
          payload: {
            request_id: "perm_1",
            tool: "edit_file",
            decision: "deny",
            resolution: "user",
          },
        },
        {
          id: "evt_7",
          run_id: "run_test",
          sequence: 7,
          schema_version: 1,
          type: "run.completed",
          created_at: "2026-08-17T00:00:02Z",
          payload: {status: "completed"},
        },
      ],
    });
    historyMocks.useRunHistory.mockReturnValue(historyState({runs: [populated]}));
    apiMocks.getRun.mockResolvedValue(populated);

    render(<App />);

    expect(await screen.findByText("I will inspect the failing test.")).toBeTruthy();
    expect(screen.getAllByText("read_file").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Permission resolved").length).toBeGreaterThan(0);
    expect(screen.getByText("7 events")).toBeTruthy();
    expect(screen.getAllByText("Completed").length).toBeGreaterThan(0);
  });

  it("hydrates the selected run before rendering its recovered timeline", async () => {
    const overview = snapshot({id: "run_overview", title: "Older run"});
    const detail = snapshot({
      id: "run_overview",
      title: "Older run",
      messages: [
        {role: "user", content: "Older run"},
        {role: "assistant", content: "Recovered from the server"},
      ],
      events: [{
        id: "evt_recovered",
        run_id: "run_overview",
        sequence: 9,
        schema_version: 1,
        type: "assistant.message",
        created_at: "2026-08-17T00:00:09Z",
        payload: {text: "Recovered from the server"},
      }],
      last_sequence: 9,
    });
    apiMocks.getRun.mockResolvedValue(detail);
    historyMocks.useRunHistory.mockReturnValue(historyState({runs: [overview]}));

    render(<App />);
    expect(await screen.findByText("Recovered from the server")).toBeTruthy();
    expect(apiMocks.getRun).toHaveBeenCalledWith("run_overview", expect.any(AbortSignal));
  });
});
