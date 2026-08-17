import type {RunEvent, RunSnapshot, WorkspaceAgent, WorkspaceTask} from "../types/runtime";


interface AgentTreeProps {
  snapshot: RunSnapshot;
  events: RunEvent[];
  agents: WorkspaceAgent[];
  tasks: WorkspaceTask[];
}

interface AgentRow {
  id: string;
  kind: "lead" | "subagent" | "teammate";
  status: string;
  taskId: string | null;
  error: string | null;
}

/** Derive ephemeral subagent lifecycle from run events and enrich teammates from SQLite. */
function deriveRows(
  snapshot: RunSnapshot,
  events: RunEvent[],
  agents: WorkspaceAgent[],
): AgentRow[] {
  const byId = new Map<string, AgentRow>([["lead", {
    id: "lead",
    kind: "lead",
    status: snapshot.status,
    taskId: null,
    error: snapshot.error,
  }]]);

  for (const event of events) {
    if (!event.type.startsWith("agent.")) continue;
    const id = typeof event.payload.agent_id === "string" ? event.payload.agent_id : "";
    if (!id) continue;
    const kind = event.payload.agent_kind === "teammate" ? "teammate" : "subagent";
    const current = byId.get(id);
    byId.set(id, {
      id,
      kind,
      status: typeof event.payload.status === "string" ? event.payload.status : current?.status ?? "unknown",
      taskId: typeof event.payload.task_id === "string" ? event.payload.task_id : current?.taskId ?? null,
      error: typeof event.payload.error === "string" ? event.payload.error : current?.error ?? null,
    });
  }
  for (const agent of agents) {
    byId.set(agent.agent_id, {
      id: agent.agent_id,
      kind: "teammate",
      status: agent.status,
      taskId: agent.current_task_id,
      error: agent.error,
    });
  }
  return [...byId.values()];
}

export function AgentTree({snapshot, events, agents, tasks}: AgentTreeProps) {
  const rows = deriveRows(snapshot, events, agents);
  const taskById = new Map(tasks.map((task) => [task.task_id, task]));

  return (
    <section className="inspector-card agent-tree" aria-label="Agent team tree">
      <header className="inspector-card__header">
        <div><p>Execution topology</p><h3>Agents</h3></div>
        <span className="source-badge source-badge--persistent">{rows.length} agents</span>
      </header>
      <ul className="agent-tree__list">
        {rows.map((agent) => {
          const task = agent.taskId ? taskById.get(agent.taskId) : undefined;
          return (
            <li className={`agent-tree__row agent-tree__row--${agent.kind}`} key={agent.id}>
              <span className="agent-tree__branch" aria-hidden="true" />
              <div className="agent-tree__copy">
                <strong>{agent.kind === "lead" ? "Lead agent" : agent.id}</strong>
                <small>
                  {agent.kind}
                  {task && ` · ${task.subject}`}
                  {task?.worktree_name && ` · ${task.worktree_name}`}
                </small>
                {agent.error && <small className="agent-tree__error">{agent.error}</small>}
              </div>
              <span className={`agent-status agent-status--${agent.status}`}>{agent.status}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
