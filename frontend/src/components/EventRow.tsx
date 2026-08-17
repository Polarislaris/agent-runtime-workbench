import type {RunEvent} from "../types/runtime";
import {formatClock, formatDuration} from "../utils/format";


function eventTitle(event: RunEvent): string {
  const tool = typeof event.payload.tool === "string" ? event.payload.tool : "Tool";
  const titles: Partial<Record<RunEvent["type"], string>> = {
    "run.started": "Run started",
    "model.started": "Model request started",
    "model.completed": "Model response received",
    "assistant.message": "Assistant message",
    "tool.started": `${tool} started`,
    "tool.completed": `${tool} completed`,
    "tool.failed": `${tool} failed`,
    "permission.requested": "Waiting for approval",
    "permission.resolved": "Permission resolved",
    "run.completed": "Run completed",
    "run.failed": "Run failed",
    "run.cancelled": "Run cancelled",
    "run.interrupted": "Run interrupted by restart",
    "task.created": "Task created",
    "task.claimed": "Task claimed",
    "task.completed": "Task completed",
    "task.failed": "Task failed",
    "agent.spawned": "Agent spawned",
    "agent.status": "Agent status changed",
    "agent.completed": "Agent completed",
    "team.message": "Team message delivered",
    "worktree.created": "Worktree created",
    "worktree.bound": "Worktree bound to task",
    "worktree.diffed": "Worktree diff inspected",
    "worktree.reviewed": "Worktree review recorded",
    "worktree.checked": "Worktree check finished",
    "worktree.committed": "Worktree committed",
    "worktree.merge_prepared": "Worktree merge prepared",
    "worktree.merged": "Worktree merged",
    "worktree.kept": "Worktree kept",
    "worktree.removed": "Worktree removed",
    "worktree.failed": "Worktree failed",
    "retry.scheduled": "Model retry scheduled",
    "context.compacted": "Context compacted",
    "background.started": "Background task started",
    "background.completed": "Background task completed",
    "cron.fired": "Scheduled task fired",
  };
  return titles[event.type] ?? event.type;
}

function eventTone(event: RunEvent): string {
  if (event.type.endsWith("failed")) return "failed";
  if (event.type === "permission.requested") return "waiting";
  if (event.type === "retry.scheduled") return "waiting";
  if (
    event.type === "agent.spawned"
    || event.type === "task.claimed"
    || event.type === "background.started"
  ) return "running";
  if (event.type.endsWith("started")) return "running";
  if (event.type === "run.cancelled") return "cancelled";
  if (event.type === "run.interrupted") return "failed";
  return "completed";
}

export function EventRow({event}: {event: RunEvent}) {
  const duration = typeof event.payload.duration_ms === "number"
    ? formatDuration(event.payload.duration_ms)
    : null;

  return (
    <details className="event-row">
      <summary>
        <span className={`event-dot event-dot--${eventTone(event)}`} aria-hidden="true" />
        <span className="event-copy">
          <strong>{eventTitle(event)}</strong>
          <small>
            {formatClock(event.created_at)}
            {duration && ` · ${duration}`}
          </small>
        </span>
        <span className="event-sequence">#{event.sequence}</span>
      </summary>
      <pre>{JSON.stringify(event.payload, null, 2)}</pre>
    </details>
  );
}
