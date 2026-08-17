import type {RunEventConnection} from "../hooks/useRunEvents";


interface ConnectionBannerProps {
  connection: RunEventConnection;
}

const CONNECTION_COPY: Record<RunEventConnection["status"], string> = {
  idle: "Runtime idle",
  connecting: "Connecting to live events",
  connected: "Live events connected",
  reconnecting: "Reconnecting to live events",
  offline: "Live connection is offline",
  "replay-complete": "Replay complete",
  closed: "Live connection closed",
};

export function ConnectionBanner({connection}: ConnectionBannerProps) {
  return (
    <div
      aria-live="polite"
      className={`connection-banner connection-banner--${connection.status}`}
      role="status"
    >
      <span aria-hidden="true" className="connection-banner__dot" />
      <span>{connection.error ?? CONNECTION_COPY[connection.status]}</span>
    </div>
  );
}
