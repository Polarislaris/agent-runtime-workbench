"""SQLite-backed request/response protocols for Agent teams.

s16 turns the s15 message bus into a small protocol layer. Messages still live
in the `messages` event log, while `protocol_requests` tracks the current state
of request/response handshakes such as graceful shutdown and plan approval.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import secrets
import sqlite3
import threading
import time
from typing import Callable, Optional

from ..config import (
    PROTOCOL_REQUEST_TIMEOUT_SECONDS,
    TEAM_AGENT_ID,
    TEAM_CLAIM_TIMEOUT_SECONDS,
    TEAM_INBOX_LIMIT,
)
from .team_bus import BUS, Message


PROTOCOL_STATUSES = {"pending", "approved", "rejected", "expired", "failed"}
FINAL_PROTOCOL_STATUSES = {"approved", "rejected", "expired", "failed"}
REQUEST_MESSAGE_TYPES = {
    "shutdown": "shutdown_request",
    "plan_approval": "plan_approval_request",
}
EXPECTED_RESPONSE_TYPES = {
    "shutdown": "shutdown_response",
    "plan_approval": "plan_approval_response",
}


@dataclass
class ProtocolState:
    """Durable state for one request/response protocol instance."""

    request_id: str
    protocol_type: str
    sender: str
    target: str
    status: str
    payload: str
    response_payload: Optional[str]
    created_at: float
    updated_at: float
    resolved_at: Optional[float]
    error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ProtocolState":
        """Convert one SQLite row into a typed protocol state record."""
        return cls(
            request_id=str(row["request_id"]),
            protocol_type=str(row["protocol_type"]),
            sender=str(row["sender"]),
            target=str(row["target"]),
            status=str(row["status"]),
            payload=str(row["payload"]),
            response_payload=row["response_payload"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            resolved_at=row["resolved_at"],
            error=row["error"],
        )

    def to_dict(self) -> dict:
        """Return a plain dict for tool output and tests."""
        return {
            "request_id": self.request_id,
            "protocol_type": self.protocol_type,
            "sender": self.sender,
            "target": self.target,
            "status": self.status,
            "payload": self.payload,
            "response_payload": self.response_payload,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "error": self.error,
        }


@dataclass
class InboxConsumption:
    """Result of routing one claimed inbox batch."""

    count: int
    text: str
    shutdown_requested: bool = False
    has_work: bool = False


class TeamProtocols:
    """Protocol state machine stored in the same SQLite DB as team messages."""

    def __init__(self):
        self._init_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        """Create protocol schema once, with indexes for common state queries."""
        BUS.initialize()
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            with BUS.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS protocol_requests (
                        request_id TEXT PRIMARY KEY,
                        protocol_type TEXT NOT NULL,
                        sender TEXT NOT NULL,
                        target TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload TEXT NOT NULL DEFAULT '',
                        response_payload TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        resolved_at REAL,
                        error TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_protocol_requests_status
                    ON protocol_requests(status, updated_at)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_protocol_requests_target
                    ON protocol_requests(target, status, updated_at)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_protocol_requests_sender
                    ON protocol_requests(sender, status, updated_at)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_protocol_requests_type
                    ON protocol_requests(protocol_type, status, updated_at)
                    """
                )

            self._initialized = True

    def new_request_id(self) -> str:
        """Create a readable request id that can safely appear in prompts."""
        return f"req_{int(time.time() * 1000)}_{secrets.token_hex(3)}"

    def _insert_message(
        self,
        conn: sqlite3.Connection,
        from_agent: str,
        to_agent: str,
        msg_type: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> int:
        """Insert a message inside an existing protocol transaction."""
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        cursor = conn.execute(
            """
            INSERT INTO messages
            (from_agent, to_agent, type, content, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (from_agent, to_agent, msg_type, content, metadata_json, time.time()),
        )
        return int(cursor.lastrowid)

    def _get_request_in_connection(
        self,
        conn: sqlite3.Connection,
        request_id: str,
    ) -> Optional[ProtocolState]:
        """Read one request with an existing connection."""
        row = conn.execute(
            """
            SELECT request_id, protocol_type, sender, target, status, payload,
                   response_payload, created_at, updated_at, resolved_at, error
            FROM protocol_requests
            WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()
        return ProtocolState.from_row(row) if row else None

    def create_request(
        self,
        protocol_type: str,
        sender: str,
        target: str,
        payload: str,
        *,
        message_type: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> ProtocolState:
        """Create a pending protocol request and send its request message atomically."""
        self.initialize()
        if protocol_type not in REQUEST_MESSAGE_TYPES:
            raise ValueError(f"Unknown protocol type: {protocol_type}")

        normalized_sender = BUS._validate_agent_id(sender)
        normalized_target = BUS._validate_agent_id(target)
        request_id = self.new_request_id()
        now = time.time()
        msg_type = message_type or REQUEST_MESSAGE_TYPES[protocol_type]
        msg_metadata = {"request_id": request_id, **(metadata or {})}

        with BUS.connection() as conn:
            try:
                # State creation and request message delivery must be atomic.
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO protocol_requests
                    (request_id, protocol_type, sender, target, status, payload,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        request_id,
                        protocol_type,
                        normalized_sender,
                        normalized_target,
                        str(payload),
                        now,
                        now,
                    ),
                )
                self._insert_message(
                    conn,
                    normalized_sender,
                    normalized_target,
                    msg_type,
                    str(payload),
                    msg_metadata,
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        return ProtocolState(
            request_id=request_id,
            protocol_type=protocol_type,
            sender=normalized_sender,
            target=normalized_target,
            status="pending",
            payload=str(payload),
            response_payload=None,
            created_at=now,
            updated_at=now,
            resolved_at=None,
            error=None,
        )

    def send_response(
        self,
        response_type: str,
        request_id: str,
        from_agent: str,
        to_agent: str,
        content: str,
        approve: bool,
        reason: str = "",
    ) -> int:
        """Send a protocol response message.

        This does not resolve the request by itself. The requester resolves it
        when the response is consumed from its inbox, preserving the message-bus
        flow described in s16.
        """
        self.initialize()
        normalized_sender = BUS._validate_agent_id(from_agent)
        normalized_target = BUS._validate_agent_id(to_agent)
        with BUS.connection() as conn:
            return self._insert_message(
                conn,
                normalized_sender,
                normalized_target,
                response_type,
                content,
                {
                    "request_id": request_id,
                    "approve": bool(approve),
                    "reason": reason,
                },
            )

    def match_response(
        self,
        response_type: str,
        request_id: str,
        from_agent: str,
        to_agent: str,
        approve: bool,
        response_payload: str = "",
    ) -> Optional[ProtocolState]:
        """Resolve a pending request only when response type and parties match."""
        self.initialize()
        with BUS.connection() as conn:
            try:
                # BEGIN IMMEDIATE keeps duplicate response consumers from both
                # reading the same pending state and racing to resolve it.
                conn.execute("BEGIN IMMEDIATE")
                state = self._get_request_in_connection(conn, request_id)
                if not state:
                    conn.execute("COMMIT")
                    return None

                expected_response = EXPECTED_RESPONSE_TYPES.get(state.protocol_type)
                parties_match = (
                    state.target == from_agent
                    and state.sender == to_agent
                )
                if (
                    state.status != "pending"
                    or response_type != expected_response
                    or not parties_match
                ):
                    conn.execute("COMMIT")
                    return state

                now = time.time()
                status = "approved" if approve else "rejected"
                conn.execute(
                    """
                    UPDATE protocol_requests
                    SET status = ?,
                        response_payload = ?,
                        updated_at = ?,
                        resolved_at = ?,
                        error = NULL
                    WHERE request_id = ?
                    AND status = 'pending'
                    """,
                    (status, response_payload, now, now, request_id),
                )
                conn.execute("COMMIT")

            except Exception:
                self._rollback_quietly(conn)
                raise

        return self.get_request(request_id)

    def review_plan(self, request_id: str, approve: bool, reason: str = "") -> ProtocolState:
        """Resolve a teammate plan request and atomically send the response."""
        self.initialize()
        with BUS.connection() as conn:
            try:
                # Updating the plan request and sending the response must be one
                # transaction so teammates never miss a resolved decision.
                conn.execute("BEGIN IMMEDIATE")
                state = self._get_request_in_connection(conn, request_id)
                if not state:
                    raise ValueError(f"Unknown protocol request: {request_id}")
                if state.protocol_type != "plan_approval":
                    raise ValueError(f"Request is not a plan_approval: {request_id}")
                if state.target != TEAM_AGENT_ID:
                    raise ValueError(f"Plan request is not addressed to {TEAM_AGENT_ID}: {request_id}")
                if state.status != "pending":
                    conn.execute("COMMIT")
                    return state

                now = time.time()
                status = "approved" if approve else "rejected"
                conn.execute(
                    """
                    UPDATE protocol_requests
                    SET status = ?,
                        response_payload = ?,
                        updated_at = ?,
                        resolved_at = ?,
                        error = NULL
                    WHERE request_id = ?
                    AND status = 'pending'
                    """,
                    (status, reason, now, now, request_id),
                )
                self._insert_message(
                    conn,
                    TEAM_AGENT_ID,
                    state.sender,
                    "plan_approval_response",
                    reason or ("Plan approved." if approve else "Plan rejected."),
                    {
                        "request_id": request_id,
                        "approve": bool(approve),
                        "reason": reason,
                    },
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        return self.get_request(request_id)  # type: ignore[return-value]

    def get_request(self, request_id: str) -> Optional[ProtocolState]:
        """Read one protocol request by id."""
        self.initialize()
        with BUS.connection() as conn:
            return self._get_request_in_connection(conn, request_id)

    def list_requests(self, status: Optional[str] = None) -> list[ProtocolState]:
        """List protocol requests, optionally filtered by status."""
        self.initialize()
        with BUS.connection() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT request_id, protocol_type, sender, target, status, payload,
                           response_payload, created_at, updated_at, resolved_at, error
                    FROM protocol_requests
                    WHERE status = ?
                    ORDER BY updated_at DESC
                    """,
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT request_id, protocol_type, sender, target, status, payload,
                           response_payload, created_at, updated_at, resolved_at, error
                    FROM protocol_requests
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        return [ProtocolState.from_row(row) for row in rows]

    def pending_count(self) -> int:
        """Return a cheap count for prompt context."""
        self.initialize()
        with BUS.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM protocol_requests WHERE status = 'pending'"
            ).fetchone()
        return int(row["count"]) if row else 0

    def expire_old_requests(
        self,
        max_age_seconds: int = PROTOCOL_REQUEST_TIMEOUT_SECONDS,
    ) -> int:
        """Move old pending protocol requests into expired terminal state."""
        self.initialize()
        cutoff = time.time() - int(max_age_seconds)
        now = time.time()
        with BUS.connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """
                    UPDATE protocol_requests
                    SET status = 'expired',
                        updated_at = ?,
                        resolved_at = ?,
                        error = 'protocol request timed out'
                    WHERE status = 'pending'
                    AND created_at < ?
                    """,
                    (now, now, cutoff),
                )
                conn.execute("COMMIT")
                return int(cursor.rowcount)
            except Exception:
                self._rollback_quietly(conn)
                raise

    def _rollback_quietly(self, conn: sqlite3.Connection) -> None:
        """Rollback if a transaction is active, preserving the original error."""
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass


PROTOCOLS = TeamProtocols()


def _format_messages_for_history(messages: list[Message]) -> str:
    """Render inbox messages with protocol metadata visible to the model."""
    lines = ["<inbox>"]
    for message in messages:
        request_id = message.metadata.get("request_id", "")
        request_text = f" request_id={request_id}" if request_id else ""
        lines.append(
            f"[message_id={message.id} from={message.from_agent} "
            f"type={message.type}{request_text}]"
        )
        lines.append(message.content)
    lines.append("</inbox>")
    return "\n".join(lines)


def request_shutdown(agent: str, reason: str = "") -> str:
    """Lead tool: ask one teammate to shut down with a tracked handshake."""
    try:
        state = PROTOCOLS.create_request(
            "shutdown",
            TEAM_AGENT_ID,
            agent,
            reason or "Please finish your current work and shut down gracefully.",
        )
    except ValueError as exc:
        return f"Error: {exc}"
    return f"Requested shutdown from {agent}: {state.request_id}"


def request_plan(agent: str, instruction: str) -> str:
    """Lead tool: ask a teammate to submit a plan before risky work.

    This is a normal directed message. The actual approval protocol begins when
    the teammate calls submit_plan, creating a plan_approval request.
    """
    try:
        message = BUS.send(
            TEAM_AGENT_ID,
            agent,
            (
                "Please submit a plan for approval before doing this work:\n"
                f"{instruction}"
            ),
            msg_type="message",
            metadata={"requested_action": "submit_plan"},
        )
    except ValueError as exc:
        return f"Error: {exc}"
    return f"Requested plan from {agent} with message {message.id}."


def submit_plan(plan: str, from_agent: str = TEAM_AGENT_ID) -> str:
    """Teammate tool: submit a plan_approval request to Lead."""
    try:
        state = PROTOCOLS.create_request(
            "plan_approval",
            from_agent,
            TEAM_AGENT_ID,
            plan,
        )
    except ValueError as exc:
        return f"Error: {exc}"
    return f"Submitted plan for approval: {state.request_id}"


def review_plan(request_id: str, approve: bool, reason: str = "") -> str:
    """Lead tool: approve or reject a pending teammate plan."""
    try:
        state = PROTOCOLS.review_plan(request_id, bool(approve), reason)
    except ValueError as exc:
        return f"Error: {exc}"
    return (
        f"Plan request {state.request_id} is {state.status}. "
        f"Response sent to {state.sender}."
    )


def list_protocol_requests(status: Optional[str] = None) -> str:
    """Lead tool: inspect protocol request state."""
    if status and status not in PROTOCOL_STATUSES:
        return f"Error: Unknown protocol status: {status}"
    requests = PROTOCOLS.list_requests(status=status)
    if not requests:
        return "(no protocol requests)"
    return "\n".join(
        json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True)
        for request in requests
    )


def consume_lead_inbox(
    history: list,
    *,
    on_appended: Callable[[dict], None] | None = None,
) -> int:
    """Claim Lead inbox, route protocol messages, append history, then ack.

    The order is deliberate: protocol state is updated before the message becomes
    visible to the model, and the message is acked only after history append
    succeeds.
    """
    BUS.release_stale_claims(TEAM_CLAIM_TIMEOUT_SECONDS)
    PROTOCOLS.expire_old_requests(PROTOCOL_REQUEST_TIMEOUT_SECONDS)
    claimed = BUS.claim_inbox(TEAM_AGENT_ID, limit=TEAM_INBOX_LIMIT)
    if not claimed:
        return 0

    message_ids = [message.id for message in claimed]
    try:
        for message in claimed:
            _dispatch_lead_message(message)
        message = {
            "role": "user",
            "content": _format_messages_for_history(claimed),
        }
        history.append(message)
        if on_appended is not None:
            on_appended(message)
    except Exception:
        BUS.release_messages(message_ids)
        raise

    BUS.ack_messages(message_ids)
    return len(claimed)


def consume_teammate_inbox(agent_id: str, history: list) -> InboxConsumption:
    """Claim one teammate inbox and route protocol messages before acking."""
    BUS.release_stale_claims(TEAM_CLAIM_TIMEOUT_SECONDS)
    PROTOCOLS.expire_old_requests(PROTOCOL_REQUEST_TIMEOUT_SECONDS)
    claimed = BUS.claim_inbox(agent_id, limit=TEAM_INBOX_LIMIT)
    if not claimed:
        return InboxConsumption(count=0, text="")

    message_ids = [message.id for message in claimed]
    shutdown_requested = False
    appendable_messages: list[Message] = []
    synthetic_notes: list[str] = []

    try:
        for message in claimed:
            result = _dispatch_teammate_message(agent_id, message)
            shutdown_requested = shutdown_requested or result.shutdown_requested
            if result.text:
                synthetic_notes.append(result.text)
            if result.has_work:
                appendable_messages.append(message)

        content_parts = []
        if appendable_messages:
            content_parts.append(_format_messages_for_history(appendable_messages))
        content_parts.extend(synthetic_notes)
        if content_parts:
            history.append({
                "role": "user",
                "content": "\n\n".join(content_parts),
            })
    except Exception:
        BUS.release_messages(message_ids)
        raise

    BUS.ack_messages(message_ids)
    return InboxConsumption(
        count=len(claimed),
        text=_format_messages_for_history(claimed),
        shutdown_requested=shutdown_requested,
        has_work=bool(appendable_messages or synthetic_notes),
    )


def _dispatch_lead_message(message: Message) -> None:
    """Route protocol responses addressed to Lead."""
    request_id = str(message.metadata.get("request_id", ""))
    if request_id and message.type.endswith("_response"):
        PROTOCOLS.match_response(
            message.type,
            request_id,
            from_agent=message.from_agent,
            to_agent=message.to_agent,
            approve=bool(message.metadata.get("approve", False)),
            response_payload=str(message.metadata.get("reason", message.content)),
        )


def _dispatch_teammate_message(agent_id: str, message: Message) -> InboxConsumption:
    """Route protocol messages addressed to one teammate."""
    request_id = str(message.metadata.get("request_id", ""))
    if message.type == "shutdown_request":
        PROTOCOLS.send_response(
            "shutdown_response",
            request_id,
            from_agent=agent_id,
            to_agent=TEAM_AGENT_ID,
            content="Shutdown acknowledged. Teammate is stopping gracefully.",
            approve=True,
            reason="graceful shutdown acknowledged",
        )
        return InboxConsumption(
            count=1,
            text="[Shutdown requested] graceful shutdown acknowledged.",
            shutdown_requested=True,
            has_work=True,
        )

    if message.type == "plan_approval_response":
        approved = bool(message.metadata.get("approve", False))
        reason = str(message.metadata.get("reason", message.content))
        status_text = "approved" if approved else "rejected"
        return InboxConsumption(
            count=1,
            text=f"[Plan {status_text}] request_id={request_id}\n{reason}",
            has_work=True,
        )

    # Normal messages/results remain visible to the teammate LLM.
    return InboxConsumption(count=1, text="", has_work=True)
