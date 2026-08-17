import {useCallback, useEffect, useMemo, useState} from "react";

import {getRunAgents, getRunTasks} from "../api/client";
import type {RunConnectionStatus} from "../hooks/useRunEvents";
import {useRunInspectorResource} from "../hooks/useRunInspectorResource";
import type {RunViewState} from "../state/runReducer";
import type {WorkspaceAgent, WorkspaceTask} from "../types/runtime";
import {formatElapsed, isActiveStatus, statusLabel} from "../utils/format";
import {AgentTree} from "./AgentTree";
import {EventRow} from "./EventRow";
import {latestLocalTodos, relatedWorktreeNames} from "./inspectorData";
import {RunSummary} from "./RunSummary";
import {TodoCard} from "./TodoCard";
import {WorktreeReviewCard} from "./WorktreeReviewCard";


type InspectorTab = "overview" | "tasks" | "agents" | "events";

interface RunInspectorProps {
  state: RunViewState;
  connection: RunConnectionStatus;
}

function ResourceError({message, onRetry}: {message: string; onRetry: () => void}) {
  return (
    <div className="inspector-resource-error" role="alert">
      <span>{message}</span>
      <button className="inspector-command" onClick={onRetry} type="button">Retry</button>
    </div>
  );
}

function LoadingRows({label}: {label: string}) {
  return (
    <div aria-label={label} className="inspector-skeleton" role="status">
      <span /><span /><span />
    </div>
  );
}

/** Stable, tabbed Inspector. Every screen is derived from snapshot/events or a lazy read API. */
export function RunInspector({state, connection}: RunInspectorProps) {
  const [now, setNow] = useState(Date.now());
  const [tab, setTab] = useState<InspectorTab>("overview");
  const snapshot = state.snapshot;
  const events = useMemo(
    () => state.orderedEventIds.map((id) => state.eventsById[id]).filter(Boolean),
    [state.eventsById, state.orderedEventIds],
  );
  const runId = snapshot?.id ?? null;
  const loadTasks = useCallback(async (id: string, signal: AbortSignal) => {
    const response = await getRunTasks(id, signal);
    return response.items;
  }, []);
  const loadAgents = useCallback(async (id: string, signal: AbortSignal) => {
    const response = await getRunAgents(id, signal);
    return response.items;
  }, []);
  const tasks = useRunInspectorResource<WorkspaceTask[]>(runId, tab === "tasks" || tab === "agents", loadTasks);
  const agents = useRunInspectorResource<WorkspaceAgent[]>(runId, tab === "agents", loadAgents);

  useEffect(() => {
    if (!snapshot || !isActiveStatus(snapshot.status)) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [snapshot]);

  if (!snapshot) {
    return (
      <aside className="run-inspector run-inspector--empty">
        <p className="panel-kicker">Run Inspector</p>
        <h2>Execution details</h2>
        <p>Select or start a run to inspect its event timeline.</p>
      </aside>
    );
  }

  const started = new Date(snapshot.started_at).getTime();
  const ended = snapshot.completed_at ? new Date(snapshot.completed_at).getTime() : now;
  const elapsed = Number.isNaN(started) ? 0 : Math.max(0, ended - started);
  const tools = Object.values(state.toolsByUseId);
  const hasMockMcp = tools.some((tool) => tool.is_mock_mcp);
  const localTodos = latestLocalTodos(events);
  const workspaceTasks = tasks.data ?? [];
  const worktreeNames = relatedWorktreeNames(events, workspaceTasks);

  return (
    <aside className="run-inspector">
      <header className="inspector-header">
        <div>
          <p className="panel-kicker">Run Inspector</p>
          <h2>{tab === "events" ? "Event audit" : "Execution details"}</h2>
        </div>
        <span className={`connection-chip connection-chip--${connection}`}>{connection}</span>
      </header>

      <div className="run-metrics">
        <div>
          <span>Status</span>
          <strong className={`metric-status metric-status--${snapshot.status}`}>{statusLabel(snapshot.status)}</strong>
        </div>
        <div><span>Elapsed</span><strong>{formatElapsed(elapsed)}</strong></div>
        <div><span>Tools</span><strong>{tools.length}</strong></div>
      </div>

      <div aria-label="Inspector views" className="inspector-tabs" role="tablist">
        {(["overview", "tasks", "agents", "events"] as InspectorTab[]).map((name) => (
          <button
            aria-controls={`inspector-${name}`}
            aria-selected={tab === name}
            className={tab === name ? "inspector-tab inspector-tab--active" : "inspector-tab"}
            key={name}
            onClick={() => setTab(name)}
            role="tab"
            type="button"
          >
            {name}
          </button>
        ))}
      </div>

      <div className="inspector-content" id={`inspector-${tab}`} role="tabpanel">
        {tab === "overview" && (
          <div className="inspector-stack">
            <RunSummary events={events} snapshot={snapshot} />
            {worktreeNames.length === 0 ? (
              <p className="inspector-empty-copy">No worktree was linked to this run.</p>
            ) : worktreeNames.map((name) => <WorktreeReviewCard key={name} worktreeName={name} />)}
            <div className="timeline-header"><span>Recent activity</span><small>{events.length} events</small></div>
            <div className="event-timeline event-timeline--compact">
              {events.slice(-8).map((event) => <EventRow event={event} key={event.id} />)}
            </div>
          </div>
        )}

        {tab === "tasks" && (
          <div className="inspector-stack">
            <TodoCard kind="local" title="This run checklist" todos={localTodos} />
            {tasks.isLoading ? <LoadingRows label="Loading persistent tasks" /> : tasks.error ? (
              <ResourceError message={tasks.error} onRetry={tasks.reload} />
            ) : <TodoCard kind="persistent" tasks={workspaceTasks} title="Persistent task board" />}
          </div>
        )}

        {tab === "agents" && (
          <div className="inspector-stack">
            {tasks.isLoading || agents.isLoading ? <LoadingRows label="Loading agent topology" /> : (
              <AgentTree agents={agents.data ?? []} events={events} snapshot={snapshot} tasks={workspaceTasks} />
            )}
            {tasks.error && <ResourceError message={tasks.error} onRetry={tasks.reload} />}
            {agents.error && <ResourceError message={agents.error} onRetry={agents.reload} />}
          </div>
        )}

        {tab === "events" && (
          <div className="event-timeline">
            {events.length === 0 ? (
              <p className="inspector-empty-copy">Waiting for the first runtime event…</p>
            ) : events.map((event) => <EventRow event={event} key={event.id} />)}
          </div>
        )}
      </div>

      {hasMockMcp && (
        <div className="mcp-note"><span className="mock-badge">Mock MCP</span><p>s19 teaching handler · no external MCP transport</p></div>
      )}
    </aside>
  );
}
