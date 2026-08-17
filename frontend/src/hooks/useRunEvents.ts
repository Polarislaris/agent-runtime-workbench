import {useEffect, useRef, useState, type Dispatch} from "react";

import type {RunAction} from "../state/runReducer";
import type {RunEvent, RunEventType, RunStatus} from "../types/runtime";


export type RunConnectionStatus =
  | "idle"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "offline"
  | "replay-complete"
  | "closed";

const RECONNECT_BASE_DELAY_MS = 500;
const RECONNECT_MAX_DELAY_MS = 8_000;
const OFFLINE_AFTER_ATTEMPTS = 3;

const BUSINESS_EVENT_TYPES: RunEventType[] = [
  "run.started",
  "model.started",
  "model.completed",
  "assistant.message",
  "tool.started",
  "tool.completed",
  "tool.failed",
  "permission.requested",
  "permission.resolved",
  "run.completed",
  "run.failed",
  "run.cancelled",
  "run.interrupted",
  "task.created",
  "task.claimed",
  "task.completed",
  "task.failed",
  "agent.spawned",
  "agent.status",
  "agent.completed",
  "team.message",
  "worktree.created",
  "worktree.bound",
  "worktree.diffed",
  "worktree.reviewed",
  "worktree.checked",
  "worktree.committed",
  "worktree.merge_prepared",
  "worktree.merged",
  "worktree.kept",
  "worktree.removed",
  "worktree.failed",
  "retry.scheduled",
  "context.compacted",
  "background.started",
  "background.completed",
  "cron.fired",
];

const TERMINAL_STATUSES: RunStatus[] = ["completed", "failed", "cancelled", "interrupted"];

export interface RunEventConnection {
  status: RunConnectionStatus;
  error: string | null;
}

export function useRunEvents(
  runId: string | null,
  status: RunStatus | null,
  lastSequence: number,
  dispatch: Dispatch<RunAction>,
): RunEventConnection {
  const sequenceRef = useRef(lastSequence);
  const cursorRunIdRef = useRef<string | null>(runId);
  const [connection, setConnection] = useState<RunEventConnection>({
    status: "idle",
    error: null,
  });

  // A reducer update can be one render behind an SSE callback.  Never move the
  // cursor backwards for the same run, but reset it when the user selects a
  // different run whose snapshot has its own durable sequence.
  if (cursorRunIdRef.current !== runId) {
    cursorRunIdRef.current = runId;
    sequenceRef.current = lastSequence;
  } else {
    sequenceRef.current = Math.max(sequenceRef.current, lastSequence);
  }
  const isTerminal = status !== null && TERMINAL_STATUSES.includes(status);

  useEffect(() => {
    if (!runId) {
      setConnection({status: "idle", error: null});
      return;
    }
    if (isTerminal) {
      // A snapshot already contains every durable event for a terminal run.
      // Do not keep an idle EventSource open merely to display old history.
      setConnection({status: "replay-complete", error: null});
      return;
    }

    let disposed = false;
    let eventSource: EventSource | null = null;
    let retryTimer: number | null = null;
    let attempts = 0;

    const handleBusinessEvent = (message: MessageEvent<string>) => {
      try {
        const event = JSON.parse(message.data) as RunEvent;
        // Keep the cursor locally as soon as an event is accepted.  React may
        // not have rendered the reducer update before an SSE error occurs.
        sequenceRef.current = Math.max(sequenceRef.current, event.sequence);
        dispatch({type: "event.received", event});
      } catch {
        setConnection((current) => ({
          ...current,
          error: "Received a malformed runtime event.",
        }));
      }
    };
    const handleHeartbeat = () => {
      setConnection((current) => ({...current, status: "connected"}));
    };

    const connect = () => {
      if (disposed) return;
      const url = `/api/runs/${encodeURIComponent(runId)}/events?after=${sequenceRef.current}`;
      eventSource = new EventSource(url);
      setConnection({
        status: attempts === 0 ? "connecting" : "reconnecting",
        error: null,
      });

      eventSource.onopen = () => {
        attempts = 0;
        setConnection({status: "connected", error: null});
      };
      eventSource.onerror = () => {
        // Close the native EventSource before scheduling our own retry.  This
        // makes the backoff predictable and lets the next URL carry the latest
        // reducer cursor instead of relying on browser-specific retry timing.
        eventSource?.close();
        if (disposed) return;
        attempts += 1;
        const delay = Math.min(
          RECONNECT_MAX_DELAY_MS,
          RECONNECT_BASE_DELAY_MS * 2 ** (attempts - 1),
        );
        setConnection({
          status: attempts >= OFFLINE_AFTER_ATTEMPTS ? "offline" : "reconnecting",
          error: null,
        });
        retryTimer = window.setTimeout(connect, delay);
      };

      for (const eventType of BUSINESS_EVENT_TYPES) {
        eventSource.addEventListener(eventType, handleBusinessEvent as EventListener);
      }
      eventSource.addEventListener("heartbeat", handleHeartbeat);
    };

    connect();

    return () => {
      disposed = true;
      eventSource?.close();
      if (retryTimer !== null) window.clearTimeout(retryTimer);
    };
  }, [dispatch, isTerminal, runId]);

  return connection;
}
