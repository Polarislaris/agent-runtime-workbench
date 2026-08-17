import type {RunEvent, RunSnapshot} from "../types/runtime";
import {formatElapsed, statusLabel} from "../utils/format";
import {deriveRunSummary} from "./inspectorData";


/** A concise, event-derived result view for the selected durable run. */
export function RunSummary({snapshot, events}: {snapshot: RunSnapshot; events: RunEvent[]}) {
  const summary = deriveRunSummary(snapshot, events);

  return (
    <section className="inspector-card run-summary" aria-label="Run summary">
      <header className="inspector-card__header">
        <div><p>Durable event summary</p><h3>Run summary</h3></div>
        <span className={`agent-status agent-status--${summary.finalStatus}`}>
          {statusLabel(summary.finalStatus)}
        </span>
      </header>
      <dl className="run-summary__facts">
        <div><dt>Elapsed</dt><dd>{formatElapsed(summary.elapsedMs)}</dd></div>
        <div><dt>Changed files</dt><dd>{summary.changedFiles.length}</dd></div>
        <div><dt>Checks</dt><dd>{summary.checks.length}</dd></div>
      </dl>
      {summary.changedFiles.length > 0 && (
        <ul className="summary-file-list">{summary.changedFiles.map((path) => <li key={path}>{path}</li>)}</ul>
      )}
      {summary.error && <p className="inspector-resource-error">{summary.error}</p>}
    </section>
  );
}
