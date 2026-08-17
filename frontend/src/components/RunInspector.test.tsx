// @vitest-environment jsdom

import {cleanup, fireEvent, render, screen, waitFor} from "@testing-library/react";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";

import {hydrateRunSnapshot} from "../state/runReducer";
import type {RunEvent, RunSnapshot} from "../types/runtime";


const apiMocks = vi.hoisted(() => ({
  ApiError: class ApiError extends Error { readonly status = 500; },
  getRunAgents: vi.fn(),
  getRunTasks: vi.fn(),
  getWorktreeChecks: vi.fn(),
  getWorktreeDiff: vi.fn(),
}));

vi.mock("../api/client", () => apiMocks);

import {RunInspector} from "./RunInspector";


function snapshot(events: RunEvent[]): RunSnapshot {
  return {
    id: "run_1",
    title: "Inspect worktree",
    status: "completed",
    messages: [],
    events,
    started_at: "2026-08-17T00:00:00Z",
    completed_at: "2026-08-17T00:00:04Z",
    error: null,
    last_sequence: events.length,
  };
}

function event(
  sequence: number,
  type: RunEvent["type"],
  payload: Record<string, unknown>,
): RunEvent {
  return {
    id: `evt_${sequence}`,
    run_id: "run_1",
    sequence,
    schema_version: 1,
    type,
    created_at: `2026-08-17T00:00:0${sequence}Z`,
    payload,
  };
}

beforeEach(() => {
  for (const mock of Object.values(apiMocks)) {
    if (typeof mock === "function" && "mockReset" in mock) mock.mockReset();
  }
  apiMocks.getRunTasks.mockResolvedValue({scope: "workspace", items: [{
    id: "task_1",
    task_id: "task_1",
    subject: "Review patch",
    description: "Review it",
    status: "in_progress",
    owner: "teammate_a",
    worktree_name: "feature-a",
    worktree: "feature-a",
    priority: 1,
    error: null,
    blockedBy: ["task_0"],
  }]});
  apiMocks.getRunAgents.mockResolvedValue({scope: "workspace", items: [{
    agent_id: "teammate_a",
    role: "reviewer",
    status: "running",
    current_task_id: "task_1",
    error: null,
  }]});
  apiMocks.getWorktreeDiff.mockResolvedValue({
    worktree_name: "feature-a",
    task_id: "task_1",
    status: "ready_for_review",
    path: "/tmp/feature-a",
    branch: "agent/feature-a",
    git_status_short: " M app.py",
    git_diff_stat: " app.py | 2 +-",
    git_diff_name_only: "app.py",
    git_diff: "diff --git a/app.py b/app.py\n+--- a/app.py\n+++ b/app.py",
  });
  apiMocks.getWorktreeChecks.mockResolvedValue({items: [{
    check_id: 1,
    worktree_name: "feature-a",
    task_id: "task_1",
    command: "pytest -q",
    exit_code: 0,
    output_preview: "passed",
    status: "passed",
    created_at: 1,
  }]});
});

afterEach(() => cleanup());

describe("RunInspector", () => {
  it("loads persistent task and teammate records only when their tabs open", async () => {
    const source = snapshot([
      event(1, "tool.started", {tool: "todo_write", input_summary: {todos: [{content: "Review patch", status: "in_progress"}]}}),
      event(2, "agent.spawned", {agent_id: "sub_1", agent_kind: "subagent", status: "running"}),
    ]);
    render(<RunInspector connection="replay-complete" state={hydrateRunSnapshot(source)} />);

    expect(apiMocks.getRunTasks).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", {name: "tasks"}));
    expect(await screen.findByText("Review patch")).toBeTruthy();
    expect(screen.getByText("Blocked by: task_0")).toBeTruthy();
    expect(apiMocks.getRunTasks).toHaveBeenCalledWith("run_1", expect.any(AbortSignal));

    fireEvent.click(screen.getByRole("tab", {name: "agents"}));
    expect(await screen.findByText("teammate_a")).toBeTruthy();
    expect(screen.getByText("Lead agent")).toBeTruthy();
    expect(apiMocks.getRunAgents).toHaveBeenCalledWith("run_1", expect.any(AbortSignal));
  });

  it("requests read-only diff and checks only after opening a worktree drawer", async () => {
    const source = snapshot([event(1, "worktree.created", {worktree_name: "feature-a"})]);
    render(<RunInspector connection="replay-complete" state={hydrateRunSnapshot(source)} />);

    expect(apiMocks.getWorktreeDiff).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", {name: "Inspect diff"}));
    expect(await screen.findByText("agent/feature-a")).toBeTruthy();
    expect(screen.getByText("pytest -q")).toBeTruthy();
    expect(apiMocks.getWorktreeDiff).toHaveBeenCalledWith("feature-a", expect.any(AbortSignal));
    expect(apiMocks.getWorktreeChecks).toHaveBeenCalledWith("feature-a", expect.any(AbortSignal));
  });

  it("keeps unknown future events inspectable in the Events tab", async () => {
    const future = event(1, "future.event" as RunEvent["type"], {value: "safe"});
    render(<RunInspector connection="replay-complete" state={hydrateRunSnapshot(snapshot([future]))} />);

    fireEvent.click(screen.getByRole("tab", {name: "events"}));
    expect(await screen.findByText("future.event")).toBeTruthy();
  });
});
