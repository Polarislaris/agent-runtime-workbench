import {describe, expect, it} from "vitest";

import type {RunEvent, RunSnapshot} from "../types/runtime";
import {deriveRunSummary, latestLocalTodos, relatedWorktreeNames} from "./inspectorData";


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

const snapshot: RunSnapshot = {
  id: "run_1",
  title: "Inspect worktree",
  status: "completed",
  messages: [],
  events: [],
  started_at: "2026-08-17T00:00:00Z",
  completed_at: "2026-08-17T00:00:04Z",
  error: null,
  last_sequence: 4,
};

describe("Inspector event-derived data", () => {
  it("uses only the latest todo_write input as the current local checklist", () => {
    const todos = latestLocalTodos([
      event(1, "tool.started", {tool: "todo_write", input_summary: {todos: [{content: "old", status: "pending"}]}}),
      event(2, "tool.started", {tool: "todo_write", input_summary: {todos: [
        {content: "Inspect diff", status: "completed"},
        {content: "Run checks", status: "in_progress"},
      ]}}),
    ]);

    expect(todos).toEqual([
      {content: "Inspect diff", status: "completed"},
      {content: "Run checks", status: "in_progress"},
    ]);
  });

  it("derives changed files, checks, and worktree names without a second store", () => {
    const events = [
      event(1, "tool.started", {tool: "write_file", input_summary: {path: "src/app.py"}}),
      event(2, "tool.started", {tool: "edit_file", input_summary: {path: "src/app.py"}}),
      event(3, "worktree.created", {worktree_name: "feature-a"}),
      event(4, "worktree.checked", {command: "pytest -q", status: "passed"}),
    ];

    expect(deriveRunSummary(snapshot, events)).toMatchObject({
      changedFiles: ["src/app.py"],
      checks: [{label: "pytest -q", status: "passed"}],
      elapsedMs: 4000,
    });
    expect(relatedWorktreeNames(events, [])).toEqual(["feature-a"]);
  });
});
