import {useCallback, useEffect, useRef, useState} from "react";

import {listRunHistory} from "../api/client";
import type {RunSnapshot, RunStatus} from "../types/runtime";


export type RunHistoryFilter = "all" | RunStatus;

export interface RunHistoryState {
  runs: RunSnapshot[];
  filter: RunHistoryFilter;
  isLoading: boolean;
  isLoadingMore: boolean;
  error: string | null;
  hasMore: boolean;
  setFilter: (filter: RunHistoryFilter) => void;
  loadMore: () => Promise<void>;
  reload: () => Promise<void>;
  upsertRun: (snapshot: RunSnapshot) => void;
}

function messageFor(caught: unknown): string {
  return caught instanceof Error ? caught.message : "Unable to load run history";
}

function mergeRuns(current: RunSnapshot[], incoming: RunSnapshot[]): RunSnapshot[] {
  const byId = new Map(current.map((run) => [run.id, run]));
  for (const run of incoming) byId.set(run.id, run);
  return [...byId.values()].sort((left, right) =>
    right.started_at.localeCompare(left.started_at) || right.id.localeCompare(left.id),
  );
}

export function useRunHistory(): RunHistoryState {
  const [filter, setFilterState] = useState<RunHistoryFilter>("all");
  const [runs, setRuns] = useState<RunSnapshot[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestRef = useRef<{id: number; controller: AbortController} | null>(null);
  const requestIdRef = useRef(0);

  const fetchPage = useCallback(async (cursor: string | null, replace: boolean) => {
    requestRef.current?.controller.abort();
    const request = {
      id: requestIdRef.current + 1,
      controller: new AbortController(),
    };
    requestIdRef.current = request.id;
    requestRef.current = request;
    if (replace) setIsLoading(true);
    else setIsLoadingMore(true);
    setError(null);

    try {
      const page = await listRunHistory({
        status: filter === "all" ? undefined : filter,
        cursor,
        limit: 25,
        signal: request.controller.signal,
      });
      if (requestRef.current?.id !== request.id) return;
      setRuns((current) => replace ? page.items : mergeRuns(current, page.items));
      setNextCursor(page.nextCursor);
    } catch (caught) {
      if (request.controller.signal.aborted || requestRef.current?.id !== request.id) return;
      setError(messageFor(caught));
    } finally {
      if (requestRef.current?.id === request.id) {
        if (replace) setIsLoading(false);
        else setIsLoadingMore(false);
      }
    }
  }, [filter]);

  const reload = useCallback(() => fetchPage(null, true), [fetchPage]);
  const loadMore = useCallback(async () => {
    if (!nextCursor || isLoading || isLoadingMore) return;
    await fetchPage(nextCursor, false);
  }, [fetchPage, isLoading, isLoadingMore, nextCursor]);

  useEffect(() => {
    void reload();
    return () => requestRef.current?.controller.abort();
  }, [reload]);

  const upsertRun = useCallback((snapshot: RunSnapshot) => {
    setRuns((current) => {
      const matchesFilter = filter === "all" || snapshot.status === filter;
      const withoutCurrent = current.filter((run) => run.id !== snapshot.id);
      return matchesFilter ? mergeRuns(withoutCurrent, [snapshot]) : withoutCurrent;
    });
  }, [filter]);

  const setFilter = useCallback((nextFilter: RunHistoryFilter) => {
    if (filter === nextFilter) return;
    // A previous page belongs to another server-side filter. Clear it before
    // the effect below fetches the replacement page so the sidebar never
    // briefly labels stale rows as matching the newly selected filter.
    setRuns([]);
    setNextCursor(null);
    setFilterState(nextFilter);
  }, [filter]);

  return {
    runs,
    filter,
    isLoading,
    isLoadingMore,
    error,
    hasMore: nextCursor !== null,
    setFilter,
    loadMore,
    reload,
    upsertRun,
  };
}
