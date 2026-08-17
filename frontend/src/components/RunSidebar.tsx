import type {RunSnapshot} from "../types/runtime";
import type {RunConnectionStatus} from "../hooks/useRunEvents";
import type {RunHistoryFilter} from "../hooks/useRunHistory";
import {formatElapsed, isActiveStatus, statusLabel} from "../utils/format";


interface RunSidebarProps {
  runs: RunSnapshot[];
  selectedRunId: string | null;
  connection: RunConnectionStatus;
  apiAvailable: boolean;
  filter: RunHistoryFilter;
  isLoading: boolean;
  isLoadingMore: boolean;
  hasMore: boolean;
  historyError: string | null;
  onNewRun: () => void;
  onSelectRun: (runId: string) => void;
  onFilterChange: (filter: RunHistoryFilter) => void;
  onLoadMore: () => void;
  onRetryHistory: () => void;
}

export function RunSidebar({
  runs,
  selectedRunId,
  connection,
  apiAvailable,
  filter,
  isLoading,
  isLoadingMore,
  hasMore,
  historyError,
  onNewRun,
  onSelectRun,
  onFilterChange,
  onLoadMore,
  onRetryHistory,
}: RunSidebarProps) {
  const hasActiveRun = runs.some((run) => isActiveStatus(run.status));
  const runtimeCopy = !apiAvailable
    ? "Runtime unavailable"
    : connection === "reconnecting"
      ? "Reconnecting"
    : connection === "offline"
      ? "Connection offline"
    : connection === "connected"
      ? "Live events connected"
      : connection === "replay-complete"
        ? "Replay complete"
      : "Local runtime ready";

  return (
    <aside className="run-sidebar">
      <div className="sidebar-brand">
        <div className="brand-mark">A</div>
        <div>
          <strong>Agent Studio</strong>
          <span>Runtime Workbench</span>
        </div>
      </div>

      <button
        className="new-run-button"
        disabled={hasActiveRun}
        onClick={onNewRun}
        type="button"
      >
        <span aria-hidden="true">＋</span> New run
      </button>

      <div className="sidebar-section-heading">
        <div className="sidebar-section-title">Runs</div>
        <select
          aria-label="Filter runs"
          onChange={(event) => onFilterChange(event.target.value as RunHistoryFilter)}
          value={filter}
        >
          <option value="all">All</option>
          <option value="queued">Queued</option>
          <option value="running">Running</option>
          <option value="waiting_permission">Awaiting approval</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
          <option value="cancelled">Cancelled</option>
          <option value="interrupted">Interrupted</option>
        </select>
      </div>
      <nav className="run-list" aria-label="Recent Agent runs">
        {isLoading ? (
          <p className="sidebar-empty">Loading runs…</p>
        ) : historyError ? (
          <div className="sidebar-history-error" role="alert">
            <span>{historyError}</span>
            <button onClick={onRetryHistory} type="button">Retry</button>
          </div>
        ) : runs.length === 0 ? (
          <p className="sidebar-empty">No runs yet.</p>
        ) : runs.map((run) => (
          <button
            className={`run-list-item ${selectedRunId === run.id ? "run-list-item--selected" : ""} ${isActiveStatus(run.status) ? "run-list-item--live" : "run-list-item--replay"}`}
            key={run.id}
            onClick={() => onSelectRun(run.id)}
            type="button"
          >
            <span className={`status-dot status-dot--${run.status}`} aria-hidden="true" />
            <span className="run-list-copy">
              <strong>{run.title}</strong>
              <small>LOCAL · {statusLabel(run.status)} · {runElapsed(run)}</small>
            </span>
          </button>
        ))}
        {hasMore && (
          <button
            className="history-load-more"
            disabled={isLoadingMore}
            onClick={onLoadMore}
            type="button"
          >
            {isLoadingMore ? "Loading…" : "Load more"}
          </button>
        )}
      </nav>

      <div className="runtime-card">
        <span className={`runtime-indicator ${apiAvailable ? "runtime-indicator--online" : ""}`} />
        <div>
          <strong>Local runtime</strong>
          <span>{runtimeCopy}</span>
        </div>
      </div>
    </aside>
  );
}

function runElapsed(run: RunSnapshot): string {
  const started = new Date(run.started_at).getTime();
  const ended = run.completed_at ? new Date(run.completed_at).getTime() : Date.now();
  if (Number.isNaN(started) || Number.isNaN(ended)) return "--:--";
  return formatElapsed(Math.max(0, ended - started));
}
