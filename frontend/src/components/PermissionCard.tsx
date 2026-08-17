import {useEffect, useState} from "react";

import type {PermissionDecision, PermissionRequest} from "../types/runtime";
import {jsonPreview} from "../utils/format";


interface PermissionCardProps {
  permission: PermissionRequest;
  onResolve: (requestId: string, decision: PermissionDecision) => Promise<void>;
}

export function PermissionCard({permission, onResolve}: PermissionCardProps) {
  const [submitted, setSubmitted] = useState<PermissionDecision | null>(null);
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (permission.status === "resolved") {
      setIsSending(false);
    }
  }, [permission.status]);

  async function decide(decision: PermissionDecision) {
    setSubmitted(decision);
    setIsSending(true);
    setError(null);
    try {
      await onResolve(permission.request_id, decision);
    } catch (caught) {
      setSubmitted(null);
      setIsSending(false);
      setError(caught instanceof Error ? caught.message : "Unable to submit decision");
    }
  }

  const isResolved = permission.status === "resolved";
  const resolvedClass = permission.decision === "allow" ? "allowed" : "denied";

  return (
    <article className={`permission-card ${isResolved ? `permission-card--${resolvedClass}` : ""}`}>
      <div className="permission-card__title">
        <span className="permission-icon" aria-hidden="true">!</span>
        <div>
          <strong>{isResolved ? "Permission resolved" : "Permission required"}</strong>
          <p>{permission.tool}</p>
        </div>
      </div>
      <p className="permission-reason">{permission.reason}</p>
      <details className="permission-input">
        <summary>Review safe input preview</summary>
        <pre>{jsonPreview(permission.args_preview)}</pre>
      </details>

      {isResolved ? (
        <div className="permission-resolution">
          {permission.decision === "allow" ? "Approved" : "Rejected"}
          {permission.resolution && ` · ${permission.resolution}`}
        </div>
      ) : (
        <div className="permission-actions">
          <button
            className="button button--secondary"
            disabled={submitted !== null || isSending}
            onClick={() => void decide("deny")}
            type="button"
          >
            {submitted === "deny" ? "Rejecting…" : "Reject"}
          </button>
          <button
            className="button button--warning"
            disabled={submitted !== null || isSending}
            onClick={() => void decide("allow")}
            type="button"
          >
            {submitted === "allow" ? "Approving…" : "Approve once"}
          </button>
          {submitted && !error && (
            <span className="permission-waiting">Waiting for runtime confirmation…</span>
          )}
        </div>
      )}
      {error && <p className="inline-error">{error}</p>}
    </article>
  );
}
