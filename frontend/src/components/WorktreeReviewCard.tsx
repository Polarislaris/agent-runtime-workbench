import {useEffect, useState} from "react";

import {ApiError, getWorktreeChecks, getWorktreeDiff} from "../api/client";
import type {WorktreeCheck, WorktreeDiff} from "../types/runtime";
import {DiffDrawer} from "./DiffDrawer";


function readableDetailError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401 || error.status === 403) return "Permission expired. Reopen the run and try again.";
    if (error.status === 404) return "Worktree no longer exists.";
    if (error.status === 422) return "Diff is unavailable for this worktree.";
  }
  return error instanceof Error ? error.message : "Unable to load worktree details.";
}

/** Lazy, read-only worktree detail. It never runs a command or mutates Git. */
export function WorktreeReviewCard({worktreeName}: {worktreeName: string}) {
  const [isOpen, setIsOpen] = useState(false);
  const [diff, setDiff] = useState<WorktreeDiff | null>(null);
  const [checks, setChecks] = useState<WorktreeCheck[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isOpen) return;
    const controller = new AbortController();
    setIsLoading(true);
    setError(null);
    // Both endpoints are snapshots. Fetching them together gives the drawer a
    // coherent read-only review view while the controller protects tab/run switches.
    Promise.all([
      getWorktreeDiff(worktreeName, controller.signal),
      getWorktreeChecks(worktreeName, controller.signal),
    ]).then(([nextDiff, nextChecks]) => {
      if (controller.signal.aborted) return;
      setDiff(nextDiff);
      setChecks(nextChecks.items);
    }).catch((caught: unknown) => {
      if (!controller.signal.aborted) setError(readableDetailError(caught));
    }).finally(() => {
      if (!controller.signal.aborted) setIsLoading(false);
    });
    return () => controller.abort();
  }, [isOpen, worktreeName]);

  return (
    <section className="inspector-card worktree-review-card" aria-label={`${worktreeName} review`}>
      <header className="inspector-card__header">
        <div><p>Isolated workspace</p><h3>{worktreeName}</h3></div>
        <span className="source-badge source-badge--persistent">worktree</span>
      </header>
      <p>Inspect the persisted worktree and recorded checks without changing Git state.</p>
      <button
        aria-expanded={isOpen}
        className="inspector-command"
        onClick={() => setIsOpen((value) => !value)}
        type="button"
      >
        {isOpen ? "Hide diff" : "Inspect diff"}
      </button>
      {isOpen && (
        <DiffDrawer
          checks={checks}
          diff={diff}
          error={error}
          isLoading={isLoading}
          onClose={() => setIsOpen(false)}
          worktreeName={worktreeName}
        />
      )}
    </section>
  );
}
