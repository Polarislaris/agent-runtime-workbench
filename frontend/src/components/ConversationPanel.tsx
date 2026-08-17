import {useEffect, useMemo, useRef} from "react";

import type {RunViewState} from "../state/runReducer";
import type {PermissionDecision, RunEvent, RunSnapshot, ToolExecution} from "../types/runtime";
import type {RunEventConnection} from "../hooks/useRunEvents";
import {statusLabel} from "../utils/format";
import {Composer} from "./Composer";
import {MessageBubble} from "./MessageBubble";
import {PermissionCard} from "./PermissionCard";
import {ToolCallCard} from "./ToolCallCard";
import {ConnectionBanner} from "./ConnectionBanner";


interface ConversationPanelProps {
  state: RunViewState;
  snapshot: RunSnapshot | null;
  isCreating: boolean;
  isStopping: boolean;
  hasActiveRun: boolean;
  isHydrating: boolean;
  connection: RunEventConnection;
  onCreate: (prompt: string) => Promise<void>;
  onStop: () => Promise<void>;
  onResolvePermission: (
    requestId: string,
    decision: PermissionDecision,
  ) => Promise<void>;
}

function eventActivity(
  event: RunEvent,
  state: RunViewState,
  onResolvePermission: ConversationPanelProps["onResolvePermission"],
) {
  if (event.type === "assistant.message") {
    const text = typeof event.payload.text === "string" ? event.payload.text : "";
    return text ? (
      <MessageBubble
        key={event.id}
        message={{role: "assistant", content: text}}
      />
    ) : null;
  }
  if (event.type === "tool.started") {
    const id = typeof event.payload.tool_use_id === "string"
      ? event.payload.tool_use_id
      : "";
    const tool = state.toolsByUseId[id];
    return tool ? <ToolCallCard key={event.id} tool={tool} /> : null;
  }
  if (event.type === "tool.failed") {
    const id = typeof event.payload.tool_use_id === "string"
      ? event.payload.tool_use_id
      : "";
    const hasStarted = Object.values(state.eventsById).some(
      (candidate) =>
        candidate.type === "tool.started" &&
        candidate.payload.tool_use_id === id,
    );
    const tool = state.toolsByUseId[id];
    return !hasStarted && tool ? <ToolCallCard key={event.id} tool={tool} /> : null;
  }
  if (event.type === "permission.requested") {
    const id = typeof event.payload.request_id === "string"
      ? event.payload.request_id
      : "";
    const permission = state.permissionsById[id];
    return permission ? (
      <PermissionCard
        key={event.id}
        onResolve={onResolvePermission}
        permission={permission}
      />
    ) : null;
  }
  return null;
}

interface ToolGroup {
  tool: "read_file" | "glob";
  tools: ToolExecution[];
  key: string;
}

type ConversationActivity = {kind: "event"; event: RunEvent} | {kind: "tool-group"; group: ToolGroup};

/**
 * Keep the conversation readable during exploration. Only consecutive,
 * repeatable lookup tools are condensed here; the Events tab still exposes
 * every original event in sequence for audit and debugging.
 */
function groupRepeatedLookups(events: RunEvent[], state: RunViewState): ConversationActivity[] {
  const result: ConversationActivity[] = [];
  let index = 0;
  while (index < events.length) {
    const current = events[index];
    const tool = current.type === "tool.started" ? current.payload.tool : null;
    if (tool !== "read_file" && tool !== "glob") {
      if (current.type !== "tool.completed" && current.type !== "tool.failed") {
        result.push({kind: "event", event: current});
      }
      index += 1;
      continue;
    }

    const grouped: ToolExecution[] = [];
    let cursor = index;
    while (cursor < events.length) {
      const candidate = events[cursor];
      if (candidate.type === "tool.started" && candidate.payload.tool === tool) {
        const id = typeof candidate.payload.tool_use_id === "string" ? candidate.payload.tool_use_id : "";
        const execution = state.toolsByUseId[id];
        if (execution) grouped.push(execution);
        cursor += 1;
        continue;
      }
      // A terminal event merely closes the preceding lookup and does not break
      // an otherwise consecutive lookup phase.
      if ((candidate.type === "tool.completed" || candidate.type === "tool.failed") && candidate.payload.tool === tool) {
        cursor += 1;
        continue;
      }
      break;
    }
    if (grouped.length > 1) {
      result.push({kind: "tool-group", group: {tool, tools: grouped, key: current.id}});
    } else {
      result.push({kind: "event", event: current});
    }
    index = cursor;
  }
  return result;
}

function GroupedToolActivity({group}: {group: ToolGroup}) {
  const failed = group.tools.some((tool) => tool.status === "failed");
  return (
    <div className={`tool-group-activity ${failed ? "tool-group-activity--failed" : ""}`}>
      <span className="tool-status-dot" aria-hidden="true" />
      <div>
        <strong>{group.tool === "read_file" ? "Read files" : "Searched files"}</strong>
        <p>{group.tools.length} consecutive {group.tool} calls</p>
      </div>
    </div>
  );
}

export function ConversationPanel({
  state,
  snapshot,
  isCreating,
  isStopping,
  hasActiveRun,
  isHydrating,
  connection,
  onCreate,
  onStop,
  onResolvePermission,
}: ConversationPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const nearBottomRef = useRef(true);
  const orderedEvents = useMemo(
    () => state.orderedEventIds.map((id) => state.eventsById[id]).filter(Boolean),
    [state.eventsById, state.orderedEventIds],
  );
  const visibleUserMessages = state.messages.filter(
    (message) => message.role === "user" && typeof message.content === "string",
  ).slice(0, 1);
  const activities = useMemo(
    () => groupRepeatedLookups(orderedEvents, state),
    [orderedEvents, state],
  );

  useEffect(() => {
    const element = scrollRef.current;
    if (element && nearBottomRef.current) {
      if (typeof element.scrollTo === "function") {
        element.scrollTo({top: element.scrollHeight, behavior: "smooth"});
      } else {
        element.scrollTop = element.scrollHeight;
      }
    }
  }, [orderedEvents.length, state.messages.length]);

  function trackScroll() {
    const element = scrollRef.current;
    if (!element) return;
    nearBottomRef.current =
      element.scrollHeight - element.scrollTop - element.clientHeight < 120;
  }

  const canCreate = !hasActiveRun;

  return (
    <section className="conversation-panel">
      <header className="panel-header conversation-header">
        <div>
          <p className="panel-kicker">Conversation</p>
          <h1>{snapshot?.title ?? "Start a new Agent run"}</h1>
          <p>{snapshot ? snapshot.id : "One focused task, fully observable"}</p>
        </div>
        {snapshot && (
          <div className="conversation-header__status">
            <ConnectionBanner connection={connection} />
            <span className={`run-status run-status--${snapshot.status}`}>
              <span aria-hidden="true" />
              {statusLabel(snapshot.status)}
            </span>
          </div>
        )}
      </header>

      <div className="conversation-scroll" onScroll={trackScroll} ref={scrollRef}>
        {isHydrating ? (
          <div className="conversation-loading" role="status">Loading run…</div>
        ) : !snapshot ? (
          <div className="welcome-state">
            <div className="welcome-mark">A</div>
            <p className="panel-kicker">Agent Runtime Workbench</p>
            <h2>See the work, not just the answer.</h2>
            <p>
              Start a coding task to watch model calls, tool execution, mock MCP,
              and approval gates unfold in real time.
            </p>
            <div className="welcome-examples">
              <span>Inspect a failing test</span>
              <span>Review a worktree</span>
              <span>Trace an Agent tool call</span>
            </div>
          </div>
        ) : (
          <div className="conversation-feed">
            {visibleUserMessages.map((message, index) => (
              <MessageBubble key={`user-${index}`} message={message} />
            ))}
            {activities.map((activity) => activity.kind === "event"
              ? eventActivity(activity.event, state, onResolvePermission)
              : <GroupedToolActivity group={activity.group} key={activity.group.key} />,
            )}
            {snapshot.status === "queued" && (
              <div className="activity-placeholder"><span /> Preparing Agent runtime…</div>
            )}
          </div>
        )}
      </div>

      <Composer
        canCreate={canCreate}
        canStop={state.canStop}
        hasRun={snapshot !== null}
        isCreating={isCreating}
        isStopping={isStopping}
        onCreate={onCreate}
        onStop={onStop}
      />
    </section>
  );
}
