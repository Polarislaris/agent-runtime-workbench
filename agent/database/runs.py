"""Durable SQLite storage primitives for Web Agent runs.

This module is the stable-version S1 persistence boundary.  It intentionally
does not publish SSE events or run Agent workers; S2 will compose this store
with a durable event sink.  Keeping storage separate lets every write use a
short SQLite transaction and keeps long-running model/tool work outside locks.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable, Optional
from uuid import uuid4

from ..config import RUN_DB
from ..runtime.events import RunEvent


SCHEMA_VERSION = 1
RUN_STATUSES = frozenset({
    "queued",
    "running",
    "waiting_permission",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
})
TERMINAL_RUN_STATUSES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "interrupted",
})
PERMISSION_STATUSES = frozenset({"pending", "approved", "rejected", "expired"})
ACTIVE_RUN_STATUSES = frozenset({"queued", "running", "waiting_permission"})
_STATUS_BY_EVENT = {
    "run.started": "running",
    "permission.requested": "waiting_permission",
    "permission.resolved": "running",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
    "run.interrupted": "interrupted",
}


class RunStoreError(RuntimeError):
    """Base error for invalid durable-run operations."""


class StoredRunNotFoundError(RunStoreError):
    """Raised when an operation references a run absent from this database."""


class EventSequenceError(RunStoreError):
    """Raised when a run event does not extend its durable sequence by one."""


class PermissionRequestNotFoundError(RunStoreError):
    """Raised when a permission record is absent from the requested run."""


class PermissionAlreadyResolvedStoreError(RunStoreError):
    """Raised when a durable permission request is no longer pending."""


@dataclass(frozen=True)
class StoredRun:
    """The durable metadata for one browser-visible Agent run."""

    id: str
    title: str
    status: str
    started_at: str
    completed_at: str | None
    error_summary: str | None
    last_sequence: int


@dataclass(frozen=True)
class StoredRunMessage:
    """One persisted model-conversation message in insertion order."""

    id: int
    run_id: str
    role: str
    content: Any
    created_at: str


@dataclass(frozen=True)
class StoredPermissionRequest:
    """A browser permission decision retained for audit and later replay."""

    id: str
    run_id: str
    tool_name: str
    input_preview: dict[str, Any]
    reason: str
    status: str
    decision: str | None
    created_at: str
    resolved_at: str | None


def _utc_now_text() -> str:
    """Return an RFC 3339 UTC timestamp suitable for durable rows."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_text(value: Any, *, field_name: str) -> str:
    """Validate JSON at the storage boundary before SQLite receives a string."""
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be JSON serializable: {error}") from error


def _json_value(raw: str, *, field_name: str) -> Any:
    """Decode a value written by this store and fail loudly on database damage."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise RunStoreError(f"invalid JSON in durable {field_name}") from error


class RunStore:
    """SQLite repository for run metadata, messages, events, and approvals.

    Each method opens its own connection.  This fits the existing thread-based
    Agent runtime: sqlite connections remain thread-bound, while WAL allows a
    browser read to proceed during a short writer transaction.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path or RUN_DB)
        self._init_lock = threading.Lock()
        self._initialized = False

    def connect(self) -> sqlite3.Connection:
        """Open one connection configured for safe local concurrent access."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield one configured connection and always close it afterwards."""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Provide an all-or-nothing writer transaction with an early write lock."""
        with self.connection() as conn:
            try:
                # BEGIN IMMEDIATE prevents two emitters from both observing the
                # same last_sequence before either one inserts its event.
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    # BEGIN may itself fail while the database is busy. Keep the
                    # original error rather than hiding it with rollback noise.
                    pass
                raise

    def initialize(self) -> None:
        """Create or migrate the database using an idempotent schema version."""
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            with self.connection() as conn:
                # WAL is persistent per database and allows readers to coexist
                # with brief writes. It must be selected before BEGIN IMMEDIATE.
                conn.execute("PRAGMA journal_mode=WAL")
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    if version > SCHEMA_VERSION:
                        raise RunStoreError(
                            "run database schema is newer than this Agent supports"
                        )
                    if version < 1:
                        self._create_v1_schema(conn)
                        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    conn.execute("COMMIT")
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass
                    raise

            self._initialized = True

    @staticmethod
    def _create_v1_schema(conn: sqlite3.Connection) -> None:
        """Create the initial stable-version run schema and supporting indexes."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'queued', 'running', 'waiting_permission', 'completed',
                    'failed', 'cancelled', 'interrupted'
                )),
                started_at TEXT NOT NULL,
                completed_at TEXT,
                error_summary TEXT,
                last_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_sequence >= 0)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_events (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 1),
                schema_version INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
                UNIQUE (run_id, sequence)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS permission_requests (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                input_preview_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'pending', 'approved', 'rejected', 'expired'
                )),
                decision TEXT CHECK (decision IN ('allow', 'deny')),
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_status_started "
            "ON runs(status, started_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_messages_run_id "
            "ON run_messages(run_id, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_events_run_sequence "
            "ON run_events(run_id, sequence)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_permission_requests_run_status "
            "ON permission_requests(run_id, status, created_at)"
        )

    def create_run(
        self,
        run_id: str,
        title: str,
        *,
        status: str = "queued",
        started_at: str | None = None,
        initial_messages: list[Mapping[str, Any]] | None = None,
    ) -> StoredRun:
        """Insert a new run and its initial prompt context atomically."""
        self.initialize()
        normalized_id = self._normalize_identifier(run_id, "run_id")
        normalized_title = str(title).strip()
        if not normalized_title:
            raise ValueError("run title must not be empty")
        normalized_status = self._normalize_run_status(status)
        timestamp = str(started_at or _utc_now_text())
        normalized_messages = [
            self._normalize_message_for_insert(message)
            for message in (initial_messages or [])
        ]

        with self._write_transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO runs (id, title, status, started_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (normalized_id, normalized_title, normalized_status, timestamp),
                )
            except sqlite3.IntegrityError as error:
                raise RunStoreError(f"run already exists: {normalized_id}") from error
            for role, content_json in normalized_messages:
                conn.execute(
                    """
                    INSERT INTO run_messages (run_id, role, content_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (normalized_id, role, content_json, timestamp),
                )
        return self.get_run(normalized_id)

    def get_run(self, run_id: str) -> StoredRun:
        """Read one durable run record without loading its messages or events."""
        self.initialize()
        normalized_id = self._normalize_identifier(run_id, "run_id")
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, title, status, started_at, completed_at, error_summary, last_sequence
                FROM runs WHERE id = ?
                """,
                (normalized_id,),
            ).fetchone()
        if row is None:
            raise StoredRunNotFoundError(f"run not found: {normalized_id}")
        return self._run_from_row(row)

    def list_runs(
        self,
        *,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> list[StoredRun]:
        """List recent runs using a stable, opaque run-id cursor.

        The ordered tuple is ``(started_at DESC, id DESC)``.  Looking up the
        cursor row first lets an API client pass only a run id while SQLite
        still applies the correct tuple comparison in one read connection.
        """
        self.initialize()
        normalized_limit = max(1, min(int(limit), 500))
        query = (
            "SELECT id, title, status, started_at, completed_at, error_summary, last_sequence "
            "FROM runs"
        )
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(self._normalize_run_status(status))
        if cursor is not None:
            normalized_cursor = self._normalize_identifier(cursor, "cursor")
            with self.connection() as conn:
                cursor_row = conn.execute(
                    "SELECT started_at, id FROM runs WHERE id = ?",
                    (normalized_cursor,),
                ).fetchone()
            if cursor_row is None:
                raise ValueError("invalid cursor")
            query += " AND" if " WHERE " in query else " WHERE"
            query += " (started_at < ? OR (started_at = ? AND id < ?))"
            params.extend([
                str(cursor_row["started_at"]),
                str(cursor_row["started_at"]),
                str(cursor_row["id"]),
            ])
        query += " ORDER BY started_at DESC, id DESC LIMIT ?"
        params.append(normalized_limit)
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._run_from_row(row) for row in rows]

    def update_run_status(
        self,
        run_id: str,
        status: str,
        *,
        completed_at: str | None = None,
        error_summary: str | None = None,
    ) -> StoredRun:
        """Persist a status transition without changing the event stream itself."""
        self.initialize()
        normalized_id = self._normalize_identifier(run_id, "run_id")
        normalized_status = self._normalize_run_status(status)
        if normalized_status in TERMINAL_RUN_STATUSES and completed_at is None:
            completed_at = _utc_now_text()

        with self._write_transaction() as conn:
            self._require_run(conn, normalized_id)
            conn.execute(
                """
                UPDATE runs
                SET status = ?, completed_at = ?, error_summary = ?
                WHERE id = ?
                """,
                (normalized_status, completed_at, error_summary, normalized_id),
            )
        return self.get_run(normalized_id)

    def append_message(
        self,
        run_id: str,
        role: str,
        content: Any,
        *,
        created_at: str | None = None,
    ) -> StoredRunMessage:
        """Append one serialized chat message in the durable insertion order."""
        self.initialize()
        normalized_id = self._normalize_identifier(run_id, "run_id")
        normalized_role = str(role).strip()
        if not normalized_role:
            raise ValueError("message role must not be empty")
        content_json = _json_text(content, field_name="message content")
        timestamp = str(created_at or _utc_now_text())

        with self._write_transaction() as conn:
            self._require_run(conn, normalized_id)
            cursor = conn.execute(
                """
                INSERT INTO run_messages (run_id, role, content_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (normalized_id, normalized_role, content_json, timestamp),
            )
            message_id = int(cursor.lastrowid)
        return StoredRunMessage(
            id=message_id,
            run_id=normalized_id,
            role=normalized_role,
            content=_json_value(content_json, field_name="message content"),
            created_at=timestamp,
        )

    def list_messages(self, run_id: str) -> list[StoredRunMessage]:
        """Load messages in the exact order in which this store accepted them."""
        self.initialize()
        normalized_id = self._normalize_identifier(run_id, "run_id")
        with self.connection() as conn:
            self._require_run(conn, normalized_id)
            rows = conn.execute(
                """
                SELECT id, run_id, role, content_json, created_at
                FROM run_messages WHERE run_id = ? ORDER BY id
                """,
                (normalized_id,),
            ).fetchall()
        return [
            StoredRunMessage(
                id=int(row["id"]),
                run_id=str(row["run_id"]),
                role=str(row["role"]),
                content=_json_value(row["content_json"], field_name="message content"),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def append_event(self, event: RunEvent) -> StoredRun:
        """Atomically append the next event and advance ``runs.last_sequence``.

        S2 will call this from a durable event sink. Keeping the invariant in
        the store now means a failed insert cannot leave metadata claiming an
        event sequence that was never committed.
        """
        self.initialize()
        with self._write_transaction() as conn:
            self._append_event_in_transaction(conn, event)
        return self.get_run(event.run_id)

    def append_next_event(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        event_id_factory: Callable[[], str],
        created_at: str | None = None,
    ) -> RunEvent:
        """Allocate, persist, and return the next run event in one transaction.

        The caller receives only a committed event. This is the S2
        ``persist -> broadcast`` boundary: a Web sink may put the returned
        event on its live queue without risking a browser-only event.
        """
        self.initialize()
        normalized_run_id = self._normalize_identifier(run_id, "run_id")
        timestamp = str(created_at or _utc_now_text())

        with self._write_transaction() as conn:
            row = self._require_run(conn, normalized_run_id)
            event = RunEvent.create(
                run_id=normalized_run_id,
                sequence=int(row["last_sequence"]) + 1,
                event_type=event_type,
                payload=dict(payload),
                clock=lambda: self._datetime_from_text(timestamp),
                event_id_factory=event_id_factory,
            )
            self._append_event_in_transaction(conn, event, known_row=row)
        return event

    def recover_interrupted_runs(self) -> list[str]:
        """Close runs abandoned by a previous API process without replaying work.

        Python threads, model requests, and shell subprocesses cannot be safely
        resumed after a process restart. Each active row is therefore converted
        to ``interrupted`` with an auditable event and any pending approval is
        expired in the same database transaction.
        """
        self.initialize()
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM runs WHERE status IN ('queued', 'running', 'waiting_permission')"
            ).fetchall()

        recovered_ids: list[str] = []
        for row in rows:
            run_id = str(row["id"])
            with self._write_transaction() as conn:
                current = self._require_run(conn, run_id)
                if str(current["status"]) not in ACTIVE_RUN_STATUSES:
                    continue
                timestamp = _utc_now_text()
                conn.execute(
                    """
                    UPDATE permission_requests
                    SET status = 'expired', resolved_at = ?
                    WHERE run_id = ? AND status = 'pending'
                    """,
                    (timestamp, run_id),
                )
                event = RunEvent.create(
                    run_id=run_id,
                    sequence=int(current["last_sequence"]) + 1,
                    event_type="run.interrupted",
                    payload={
                        "status": "interrupted",
                        "reason": "Agent API restarted before this run finished.",
                    },
                    clock=lambda: self._datetime_from_text(timestamp),
                    event_id_factory=lambda: f"evt_recovered_{uuid4().hex}",
                )
                self._append_event_in_transaction(conn, event, known_row=current)
                recovered_ids.append(run_id)
        return recovered_ids

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1_000,
    ) -> list[RunEvent]:
        """Load durable events after a cursor for later SSE catch-up support."""
        self.initialize()
        normalized_id = self._normalize_identifier(run_id, "run_id")
        normalized_after = max(0, int(after_sequence))
        normalized_limit = max(1, min(int(limit), 5_000))
        with self.connection() as conn:
            self._require_run(conn, normalized_id)
            rows = conn.execute(
                """
                SELECT id, run_id, sequence, schema_version, event_type, payload_json, created_at
                FROM run_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (normalized_id, normalized_after, normalized_limit),
            ).fetchall()
        return [
            RunEvent(
                id=str(row["id"]),
                run_id=str(row["run_id"]),
                sequence=int(row["sequence"]),
                schema_version=int(row["schema_version"]),
                type=str(row["event_type"]),
                created_at=str(row["created_at"]),
                payload=self._payload_from_row(row),
            )
            for row in rows
        ]

    def event_sequence(self, run_id: str, event_id: str) -> int | None:
        """Look up an opaque SSE event id without exposing raw SQL to routes."""
        self.initialize()
        normalized_run_id = self._normalize_identifier(run_id, "run_id")
        normalized_event_id = self._normalize_identifier(event_id, "event_id")
        with self.connection() as conn:
            self._require_run(conn, normalized_run_id)
            row = conn.execute(
                "SELECT sequence FROM run_events WHERE run_id = ? AND id = ?",
                (normalized_run_id, normalized_event_id),
            ).fetchone()
        return int(row["sequence"]) if row is not None else None

    def create_permission_request(
        self,
        request_id: str,
        run_id: str,
        tool_name: str,
        input_preview: Mapping[str, Any],
        reason: str,
        *,
        created_at: str | None = None,
    ) -> StoredPermissionRequest:
        """Persist a pending permission before a worker begins waiting for it."""
        self.initialize()
        normalized_request_id = self._normalize_identifier(request_id, "request_id")
        normalized_run_id = self._normalize_identifier(run_id, "run_id")
        normalized_tool_name = str(tool_name).strip()
        normalized_reason = str(reason).strip()
        if not normalized_tool_name or not normalized_reason:
            raise ValueError("permission tool_name and reason must not be empty")
        preview_json = _json_text(dict(input_preview), field_name="permission input_preview")
        timestamp = str(created_at or _utc_now_text())

        with self._write_transaction() as conn:
            self._require_run(conn, normalized_run_id)
            try:
                conn.execute(
                    """
                    INSERT INTO permission_requests
                    (id, run_id, tool_name, input_preview_json, reason, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        normalized_request_id,
                        normalized_run_id,
                        normalized_tool_name,
                        preview_json,
                        normalized_reason,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise RunStoreError(
                    f"permission request already exists: {normalized_request_id}"
                ) from error
        return self.get_permission_request(normalized_run_id, normalized_request_id)

    def resolve_permission_request(
        self,
        run_id: str,
        request_id: str,
        decision: str,
        *,
        resolved_at: str | None = None,
    ) -> StoredPermissionRequest:
        """Resolve exactly one pending approval; S9 will wire this to workers."""
        self.initialize()
        normalized_run_id = self._normalize_identifier(run_id, "run_id")
        normalized_request_id = self._normalize_identifier(request_id, "request_id")
        normalized_decision = str(decision).strip()
        if normalized_decision not in {"allow", "deny"}:
            raise ValueError("permission decision must be 'allow' or 'deny'")
        resolved_status = "approved" if normalized_decision == "allow" else "rejected"
        timestamp = str(resolved_at or _utc_now_text())

        with self._write_transaction() as conn:
            row = self._require_permission_request(
                conn, normalized_run_id, normalized_request_id
            )
            if str(row["status"]) != "pending":
                raise PermissionAlreadyResolvedStoreError(
                    f"permission request already resolved: {normalized_request_id}"
                )
            conn.execute(
                """
                UPDATE permission_requests
                SET status = ?, decision = ?, resolved_at = ?
                WHERE id = ? AND run_id = ?
                """,
                (
                    resolved_status,
                    normalized_decision,
                    timestamp,
                    normalized_request_id,
                    normalized_run_id,
                ),
            )
        return self.get_permission_request(normalized_run_id, normalized_request_id)

    def expire_pending_permissions(self, run_id: str, *, resolved_at: str | None = None) -> int:
        """Mark abandoned permissions expired during the S3 startup recovery path."""
        self.initialize()
        normalized_run_id = self._normalize_identifier(run_id, "run_id")
        timestamp = str(resolved_at or _utc_now_text())
        with self._write_transaction() as conn:
            self._require_run(conn, normalized_run_id)
            cursor = conn.execute(
                """
                UPDATE permission_requests
                SET status = 'expired', resolved_at = ?
                WHERE run_id = ? AND status = 'pending'
                """,
                (timestamp, normalized_run_id),
            )
            return int(cursor.rowcount)

    def get_permission_request(
        self,
        run_id: str,
        request_id: str,
    ) -> StoredPermissionRequest:
        """Read one approval record for replay or an HTTP response."""
        self.initialize()
        normalized_run_id = self._normalize_identifier(run_id, "run_id")
        normalized_request_id = self._normalize_identifier(request_id, "request_id")
        with self.connection() as conn:
            row = self._require_permission_request(
                conn, normalized_run_id, normalized_request_id
            )
        return self._permission_from_row(row)

    def list_permission_requests(self, run_id: str) -> list[StoredPermissionRequest]:
        """Load a run's approval audit trail in creation order for REST replay."""
        self.initialize()
        normalized_run_id = self._normalize_identifier(run_id, "run_id")
        with self.connection() as conn:
            self._require_run(conn, normalized_run_id)
            rows = conn.execute(
                """
                SELECT id, run_id, tool_name, input_preview_json, reason, status,
                       decision, created_at, resolved_at
                FROM permission_requests
                WHERE run_id = ?
                ORDER BY created_at, id
                """,
                (normalized_run_id,),
            ).fetchall()
        return [self._permission_from_row(row) for row in rows]

    @staticmethod
    def _normalize_identifier(value: str, field_name: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{field_name} must not be empty")
        return normalized

    @staticmethod
    def _normalize_run_status(status: str) -> str:
        normalized = str(status).strip()
        if normalized not in RUN_STATUSES:
            allowed = ", ".join(sorted(RUN_STATUSES))
            raise ValueError(f"invalid run status {normalized!r}; expected one of: {allowed}")
        return normalized

    @staticmethod
    def _normalize_message_for_insert(message: Mapping[str, Any]) -> tuple[str, str]:
        """Validate one serialized chat message used in a create transaction."""
        role = str(message.get("role", "")).strip()
        if not role:
            raise ValueError("message role must not be empty")
        if "content" not in message:
            raise ValueError("message content must be present")
        return role, _json_text(message["content"], field_name="message content")

    def _append_event_in_transaction(
        self,
        conn: sqlite3.Connection,
        event: RunEvent,
        *,
        known_row: sqlite3.Row | None = None,
    ) -> None:
        """Insert one event and matching run metadata inside an active transaction."""
        row = known_row or self._require_run(conn, event.run_id)
        expected_sequence = int(row["last_sequence"]) + 1
        if event.sequence != expected_sequence:
            raise EventSequenceError(
                f"run {event.run_id} expected event sequence {expected_sequence}, "
                f"got {event.sequence}"
            )
        try:
            conn.execute(
                """
                INSERT INTO run_events
                (id, run_id, sequence, schema_version, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.run_id,
                    event.sequence,
                    event.schema_version,
                    event.type,
                    _json_text(event.payload, field_name="event payload"),
                    event.created_at,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise RunStoreError(f"duplicate durable event id: {event.id}") from error

        status = _STATUS_BY_EVENT.get(event.type)
        if status is None:
            conn.execute(
                "UPDATE runs SET last_sequence = ? WHERE id = ?",
                (event.sequence, event.run_id),
            )
            return

        completed_at = event.created_at if status in TERMINAL_RUN_STATUSES else None
        error_summary = (
            str(event.payload.get("error", "")) or None
            if status == "failed"
            else None
        )
        conn.execute(
            """
            UPDATE runs
            SET status = ?, completed_at = ?, error_summary = ?, last_sequence = ?
            WHERE id = ?
            """,
            (status, completed_at, error_summary, event.sequence, event.run_id),
        )

    @staticmethod
    def _datetime_from_text(timestamp: str) -> datetime:
        """Parse the store's RFC 3339 timestamps for RunEvent validation."""
        normalized = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            raise ValueError("durable event timestamps must be timezone-aware")
        return parsed

    @staticmethod
    def _require_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT id, title, status, started_at, completed_at, error_summary, last_sequence
            FROM runs WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise StoredRunNotFoundError(f"run not found: {run_id}")
        return row

    @staticmethod
    def _require_permission_request(
        conn: sqlite3.Connection,
        run_id: str,
        request_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT id, run_id, tool_name, input_preview_json, reason, status,
                   decision, created_at, resolved_at
            FROM permission_requests WHERE run_id = ? AND id = ?
            """,
            (run_id, request_id),
        ).fetchone()
        if row is None:
            raise PermissionRequestNotFoundError(
                f"permission request not found: {request_id}"
            )
        return row

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> StoredRun:
        return StoredRun(
            id=str(row["id"]),
            title=str(row["title"]),
            status=str(row["status"]),
            started_at=str(row["started_at"]),
            completed_at=(str(row["completed_at"]) if row["completed_at"] else None),
            error_summary=(
                str(row["error_summary"]) if row["error_summary"] else None
            ),
            last_sequence=int(row["last_sequence"]),
        )

    @staticmethod
    def _payload_from_row(row: sqlite3.Row) -> dict[str, Any]:
        payload = _json_value(row["payload_json"], field_name="event payload")
        if not isinstance(payload, dict):
            raise RunStoreError("durable event payload must be a JSON object")
        return payload

    @staticmethod
    def _permission_from_row(row: sqlite3.Row) -> StoredPermissionRequest:
        preview = _json_value(row["input_preview_json"], field_name="permission input_preview")
        if not isinstance(preview, dict):
            raise RunStoreError("durable permission input_preview must be a JSON object")
        return StoredPermissionRequest(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            tool_name=str(row["tool_name"]),
            input_preview=preview,
            reason=str(row["reason"]),
            status=str(row["status"]),
            decision=(str(row["decision"]) if row["decision"] else None),
            created_at=str(row["created_at"]),
            resolved_at=(str(row["resolved_at"]) if row["resolved_at"] else None),
        )
