import {useEffect, useState} from "react";

import type {WorktreeCheck, WorktreeDiff} from "../types/runtime";


interface DiffDrawerProps {
  worktreeName: string;
  diff: WorktreeDiff | null;
  checks: WorktreeCheck[];
  isLoading: boolean;
  error: string | null;
  onClose: () => void;
}

const LINES_PER_PAGE = 120;

/**
 * Render a large patch in bounded line pages. The API already truncates the
 * payload; this second guard keeps an unusually large, valid response from
 * freezing the Inspector or changing its fixed-height layout.
 */
export function DiffDrawer({
  worktreeName,
  diff,
  checks,
  isLoading,
  error,
  onClose,
}: DiffDrawerProps) {
  const lines = (diff?.git_diff ?? "").split("\n");
  const [visibleLines, setVisibleLines] = useState(LINES_PER_PAGE);

  useEffect(() => setVisibleLines(LINES_PER_PAGE), [worktreeName, diff?.git_diff]);

  return (
    <section className="diff-drawer" aria-label={`${worktreeName} diff`}>
      <header className="diff-drawer__header">
        <div><p>Read-only review</p><h4>{worktreeName}</h4></div>
        <button aria-label="Close diff" className="icon-button" onClick={onClose} type="button">×</button>
      </header>
      {isLoading ? (
        <div className="inspector-skeleton" role="status"><span /><span /><span /></div>
      ) : error ? (
        <p className="inspector-resource-error" role="alert">{error}</p>
      ) : !diff ? (
        <p className="inspector-empty-copy">Diff is unavailable for this worktree.</p>
      ) : (
        <>
          <div className="diff-meta">
            <span>{diff.branch}</span>
            <span>{diff.status}</span>
            <span>{diff.git_diff_name_only || "No changed files"}</span>
          </div>
          <pre className="diff-drawer__patch">{lines.slice(0, visibleLines).join("\n") || "No uncommitted diff."}</pre>
          {visibleLines < lines.length && (
            <button className="inspector-command" onClick={() => setVisibleLines((value) => value + LINES_PER_PAGE)} type="button">
              Show next {Math.min(LINES_PER_PAGE, lines.length - visibleLines)} lines
            </button>
          )}
          <div className="diff-checks">
            <strong>Recorded checks</strong>
            {checks.length === 0 ? <span>No persisted checks.</span> : checks.map((check) => (
              <div key={check.check_id}>
                <span className={`check-state check-state--${check.status}`} aria-hidden="true" />
                <span>{check.command}</span><small>exit {check.exit_code}</small>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
