import type {ToolExecution} from "../types/runtime";
import {formatDuration, jsonPreview} from "../utils/format";


export function ToolCallCard({tool}: {tool: ToolExecution}) {
  const detail = tool.error ?? tool.output_preview;
  return (
    <article className={`tool-card tool-card--${tool.status}`}>
      <div className="tool-card__header">
        <span className="tool-status-dot" aria-hidden="true" />
        <strong>{tool.tool}</strong>
        {tool.is_mock_mcp && <span className="mock-badge">Mock MCP</span>}
        <span className={`status-badge status-badge--${tool.status}`}>
          {tool.status}
          {tool.duration_ms !== undefined && ` · ${formatDuration(tool.duration_ms)}`}
        </span>
      </div>
      <pre className="tool-input">{jsonPreview(tool.input_summary)}</pre>
      {detail && (
        <details className="tool-result">
          <summary>{tool.error ? "Show error" : "Show result"}</summary>
          <pre>{detail}</pre>
        </details>
      )}
    </article>
  );
}
