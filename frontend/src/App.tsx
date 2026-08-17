import {useCallback, useEffect, useReducer, useRef, useState} from "react";

import {
  cancelRun,
  createRun,
  getRun,
  resolvePermission,
} from "./api/client";
import {ConversationPanel} from "./components/ConversationPanel";
import {RunInspector} from "./components/RunInspector";
import {RunSidebar} from "./components/RunSidebar";
import {useRunHistory} from "./hooks/useRunHistory";
import {useRunEvents} from "./hooks/useRunEvents";
import {
  createInitialRunState,
  runReducer,
} from "./state/runReducer";
import type {PermissionDecision, RunSnapshot} from "./types/runtime";
import {isActiveStatus} from "./utils/format";

export function App() {
  const history = useRunHistory();
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [state, dispatch] = useReducer(runReducer, undefined, createInitialRunState);
  const [isCreating, setIsCreating] = useState(false);
  const [isStopping, setIsStopping] = useState(false);
  const [isHydrating, setIsHydrating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selectionRequestRef = useRef<AbortController | null>(null);
  const didAutoSelectRef = useRef(false);

  const streamRunId = state.snapshot?.id === selectedRunId
    ? selectedRunId
    : null;
  const connection = useRunEvents(
    streamRunId,
    state.snapshot?.status ?? null,
    state.lastSequence,
    dispatch,
  );
  const hasActiveRun = history.runs.some((run) => isActiveStatus(run.status))
    || (state.snapshot ? isActiveStatus(state.snapshot.status) : false);

  useEffect(() => {
    if (!state.snapshot) return;
    history.upsertRun(state.snapshot);
  }, [history.upsertRun, state.snapshot]);

  const selectRun = useCallback(async (runId: string) => {
    selectionRequestRef.current?.abort();
    const controller = new AbortController();
    selectionRequestRef.current = controller;
    setError(null);
    setSelectedRunId(runId);
    setIsHydrating(true);
    // Do not display the previous run while a different snapshot loads. This
    // also guarantees useRunEvents waits for the selected snapshot cursor.
    dispatch({type: "reset"});
    try {
      const snapshot = await getRun(runId, controller.signal);
      if (selectionRequestRef.current !== controller) return;
      dispatch({type: "snapshot.hydrated", snapshot});
    } catch (caught) {
      if (controller.signal.aborted || selectionRequestRef.current !== controller) return;
      setError(caught instanceof Error ? caught.message : "Unable to load run");
      setSelectedRunId(null);
    } finally {
      if (selectionRequestRef.current === controller) setIsHydrating(false);
    }
  }, []);

  useEffect(() => {
    if (didAutoSelectRef.current || history.isLoading || !history.runs[0]) return;
    didAutoSelectRef.current = true;
    void selectRun(history.runs[0].id);
  }, [history.isLoading, history.runs, selectRun]);

  useEffect(() => () => selectionRequestRef.current?.abort(), []);

  async function handleCreate(prompt: string) {
    setIsCreating(true);
    setError(null);
    try {
      const snapshot = await createRun(prompt);
      selectionRequestRef.current?.abort();
      history.upsertRun(snapshot);
      dispatch({type: "snapshot.hydrated", snapshot});
      setSelectedRunId(snapshot.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unable to create run");
      throw caught;
    } finally {
      setIsCreating(false);
    }
  }

  async function handleStop() {
    if (!selectedRunId) return;
    setIsStopping(true);
    setError(null);
    try {
      const snapshot = await cancelRun(selectedRunId);
      dispatch({type: "snapshot.hydrated", snapshot});
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unable to stop run");
    } finally {
      setIsStopping(false);
    }
  }

  async function handlePermission(
    requestId: string,
    decision: PermissionDecision,
  ) {
    if (!selectedRunId) return;
    await resolvePermission(selectedRunId, requestId, decision);
  }

  function handleNewRun() {
    if (hasActiveRun) return;
    selectionRequestRef.current?.abort();
    setSelectedRunId(null);
    dispatch({type: "reset"});
    history.setFilter("all");
    setError(null);
  }

  return (
    <main className="workbench-shell">
      <div className="window-bar">
        <div className="window-dots" aria-hidden="true"><span /><span /><span /></div>
        <span>Agent Runtime Workbench · Stable</span>
        <span className="window-mode">LOCAL</span>
      </div>
      <div className="workbench-grid">
        <RunSidebar
          apiAvailable={history.error === null}
          connection={connection.status}
          filter={history.filter}
          hasMore={history.hasMore}
          historyError={history.error}
          isLoading={history.isLoading}
          isLoadingMore={history.isLoadingMore}
          onFilterChange={history.setFilter}
          onLoadMore={() => void history.loadMore()}
          onNewRun={handleNewRun}
          onSelectRun={(runId) => void selectRun(runId)}
          onRetryHistory={() => void history.reload()}
          runs={history.runs}
          selectedRunId={selectedRunId}
        />
        <ConversationPanel
          connection={connection}
          hasActiveRun={hasActiveRun}
          isHydrating={isHydrating}
          isCreating={isCreating}
          isStopping={isStopping}
          onCreate={handleCreate}
          onResolvePermission={handlePermission}
          onStop={handleStop}
          snapshot={state.snapshot}
          state={state}
        />
        <RunInspector connection={connection.status} state={state} />
      </div>
      {error && (
        <div className="error-toast" role="alert">
          <strong>Runtime notice</strong>
          <span>{error}</span>
          <button onClick={() => setError(null)} type="button" aria-label="Dismiss">×</button>
        </div>
      )}
    </main>
  );
}
