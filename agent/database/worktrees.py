"""SQLite worktree registry for s18 task directory isolation.

Git worktree commands are external side effects and live in features/worktree.py.
This module records durable state that can be kept transactionally: which task
is bound to which worktree, where that worktree lives, and what happened to it
over time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading
import time
from typing import Optional

from .autonomous_tasks import TASK_STORE
from .team_bus import BUS


WORKTREE_STATUSES = {
    "active",
    "ready_for_review",
    "needs_changes",
    "approved",
    "committed",
    "merged",
    "kept",
    "removed",
    "failed",
}
WORKTREE_EVENT_TYPES = {
    "created",
    "bound",
    "diffed",
    "reviewed",
    "ready_for_review",
    "needs_changes",
    "approved",
    "checked",
    "committed",
    "merge_prepared",
    "merged",
    "merge_failed",
    "kept",
    "removed",
    "failed",
}
BOUND_WORKTREE_STATUSES = {
    "active",
    "ready_for_review",
    "needs_changes",
    "approved",
    "committed",
    "merged",
    "kept",
}


@dataclass
class WorktreeRecord:
    """Durable state for one Git worktree."""

    worktree_name: str
    task_id: Optional[str]
    path: str
    branch: str
    status: str
    created_at: float
    updated_at: float
    kept_at: Optional[float]
    removed_at: Optional[float]
    approved_at: Optional[float]
    committed_at: Optional[float]
    merged_at: Optional[float]
    error: Optional[str]

    def to_dict(self) -> dict:
        """Return a JSON-friendly representation for tool output."""
        return {
            "worktree_name": self.worktree_name,
            "task_id": self.task_id,
            "path": self.path,
            "branch": self.branch,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "kept_at": self.kept_at,
            "removed_at": self.removed_at,
            "approved_at": self.approved_at,
            "committed_at": self.committed_at,
            "merged_at": self.merged_at,
            "error": self.error,
        }


@dataclass
class WorktreeEventRecord:
    """Immutable audit event for worktree lifecycle changes."""

    event_id: int
    worktree_name: str
    task_id: Optional[str]
    event_type: str
    message: str
    created_at: float

    def to_dict(self) -> dict:
        """Return a JSON-friendly event representation."""
        return {
            "event_id": self.event_id,
            "worktree_name": self.worktree_name,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "message": self.message,
            "created_at": self.created_at,
        }


class WorktreeStore:
    """Transactional SQLite registry for Git worktrees."""

    def __init__(self):
        self._init_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        """Create s18 worktree tables and migrate older schemas."""
        TASK_STORE.initialize()
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            with BUS.connection() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                self._ensure_column(conn, "tasks", "worktree_name", "TEXT")
                self._migrate_worktrees_schema(conn)
                self._migrate_worktree_events_schema(conn)
                self._create_worktrees_table(conn)
                self._create_worktree_events_table(conn)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_worktrees_task "
                    "ON worktrees(task_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_worktrees_status "
                    "ON worktrees(status, updated_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_worktree_events_worktree "
                    "ON worktree_events(worktree_name, created_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_worktree_events_task "
                    "ON worktree_events(task_id, created_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_worktree_events_type "
                    "ON worktree_events(event_type, created_at)"
                )

            self._initialized = True

    def create_record(
        self,
        worktree_name: str,
        path: Path,
        branch: str,
        task_id: str = "",
    ) -> WorktreeRecord:
        """Record a newly-created Git worktree and optional task binding."""
        self.initialize()
        normalized_task_id = str(task_id or "").strip() or None
        now = time.time()

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if normalized_task_id:
                    self._validate_bindable_task(conn, normalized_task_id)
                    self._validate_task_has_no_other_worktree(
                        conn,
                        normalized_task_id,
                        worktree_name,
                    )

                conn.execute(
                    """
                    INSERT INTO worktrees
                    (worktree_name, task_id, path, branch, status, created_at,
                     updated_at, kept_at, removed_at, approved_at, committed_at,
                     merged_at, error)
                    VALUES (?, ?, ?, ?, 'active', ?, ?, NULL, NULL, NULL, NULL, NULL, NULL)
                    """,
                    (worktree_name, normalized_task_id, str(path), branch, now, now),
                )
                self._insert_event(
                    conn,
                    worktree_name,
                    normalized_task_id,
                    "created",
                    f"Created worktree {worktree_name} at {path}",
                    now,
                )
                if normalized_task_id:
                    conn.execute(
                        """
                        UPDATE tasks
                        SET worktree_name = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (worktree_name, now, normalized_task_id),
                    )
                    self._insert_event(
                        conn,
                        worktree_name,
                        normalized_task_id,
                        "bound",
                        f"Bound task {normalized_task_id} to worktree {worktree_name}",
                        now,
                    )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        record = self.get_worktree(worktree_name)
        if not record:
            raise RuntimeError(f"Worktree disappeared after create: {worktree_name}")
        return record

    def bind_task(self, task_id: str, worktree_name: str) -> WorktreeRecord:
        """Bind an existing active worktree to a pending task atomically."""
        self.initialize()
        normalized_task_id = str(task_id).strip()
        now = time.time()

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._validate_bindable_task(conn, normalized_task_id)
                row = self._get_worktree_row(conn, worktree_name)
                if not row:
                    raise ValueError(f"Worktree not found: {worktree_name}")
                if row["status"] != "active":
                    raise ValueError(f"Worktree {worktree_name} is {row['status']}, cannot bind")
                if row["task_id"] and row["task_id"] != normalized_task_id:
                    raise ValueError(f"Worktree {worktree_name} already bound to {row['task_id']}")
                self._validate_task_has_no_other_worktree(conn, normalized_task_id, worktree_name)

                conn.execute(
                    """
                    UPDATE worktrees
                    SET task_id = ?, updated_at = ?, error = NULL
                    WHERE worktree_name = ?
                    """,
                    (normalized_task_id, now, worktree_name),
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET worktree_name = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (worktree_name, now, normalized_task_id),
                )
                self._insert_event(
                    conn,
                    worktree_name,
                    normalized_task_id,
                    "bound",
                    f"Bound task {normalized_task_id} to worktree {worktree_name}",
                    now,
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        record = self.get_worktree(worktree_name)
        if not record:
            raise RuntimeError(f"Worktree disappeared after bind: {worktree_name}")
        return record

    def get_worktree(self, worktree_name: str) -> Optional[WorktreeRecord]:
        """Read one worktree by name."""
        self.initialize()
        with BUS.connection() as conn:
            row = self._get_worktree_row(conn, worktree_name)
        return self._record_from_row(row) if row else None

    def get_task_worktree(self, task_id: str) -> Optional[WorktreeRecord]:
        """Read the current durable worktree bound to a task."""
        self.initialize()
        placeholders = ",".join("?" for _ in BOUND_WORKTREE_STATUSES)
        params: list[object] = [str(task_id).strip(), *sorted(BOUND_WORKTREE_STATUSES)]
        with BUS.connection() as conn:
            row = conn.execute(
                f"""
                SELECT worktree_name, task_id, path, branch, status, created_at,
                       updated_at, kept_at, removed_at, approved_at, committed_at,
                       merged_at, error
                FROM worktrees
                WHERE task_id = ?
                AND status IN ({placeholders})
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return self._record_from_row(row) if row else None

    def list_worktrees(self, status: Optional[str] = None) -> list[WorktreeRecord]:
        """List known worktrees, optionally filtered by status."""
        self.initialize()
        params: list[object] = []
        where = ""
        if status:
            normalized_status = str(status).strip()
            if normalized_status not in WORKTREE_STATUSES:
                raise ValueError(f"invalid worktree status: {normalized_status}")
            where = "WHERE status = ?"
            params.append(normalized_status)

        with BUS.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT worktree_name, task_id, path, branch, status, created_at,
                       updated_at, kept_at, removed_at, approved_at, committed_at,
                       merged_at, error
                FROM worktrees
                {where}
                ORDER BY updated_at DESC, worktree_name ASC
                """,
                params,
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def mark_ready_for_review(self, task_id: str) -> Optional[WorktreeRecord]:
        """Mark a bound active worktree ready after its task is completed."""
        return self._set_status_by_task(
            task_id,
            "ready_for_review",
            "ready_for_review",
            "Task completed; worktree is ready for review",
            allowed_statuses={"active", "needs_changes", "approved"},
        )

    def mark_needs_changes(self, worktree_name: str, message: str = "") -> WorktreeRecord:
        """Mark a reviewed worktree as needing more changes."""
        return self.set_status(
            worktree_name,
            "needs_changes",
            event_type="needs_changes",
            message=message or f"Worktree {worktree_name} needs changes",
        )

    def mark_approved(self, worktree_name: str, message: str = "") -> WorktreeRecord:
        """Mark a reviewed worktree as approved."""
        return self.set_status(
            worktree_name,
            "approved",
            event_type="approved",
            message=message or f"Worktree {worktree_name} approved",
        )

    def mark_committed(self, worktree_name: str, message: str = "") -> WorktreeRecord:
        """Mark a worktree as committed after Git commit succeeds."""
        return self.set_status(
            worktree_name,
            "committed",
            event_type="committed",
            message=message or f"Worktree {worktree_name} committed",
        )

    def mark_merged(self, worktree_name: str, message: str = "") -> WorktreeRecord:
        """Mark a worktree as merged after Git merge succeeds."""
        return self.set_status(
            worktree_name,
            "merged",
            event_type="merged",
            message=message or f"Worktree {worktree_name} merged",
        )

    def keep_worktree(self, worktree_name: str) -> WorktreeRecord:
        """Mark a worktree as kept for review without touching Git files."""
        return self.set_status(
            worktree_name,
            "kept",
            event_type="kept",
            message=f"Kept worktree {worktree_name} for review",
        )

    def mark_removed(self, worktree_name: str) -> WorktreeRecord:
        """Mark a worktree removed and clear its task binding."""
        self.initialize()
        now = time.time()
        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_worktree_row(conn, worktree_name)
                if not row:
                    raise ValueError(f"Worktree not found: {worktree_name}")
                task_id = row["task_id"]
                conn.execute(
                    """
                    UPDATE worktrees
                    SET status = 'removed',
                        removed_at = ?,
                        updated_at = ?,
                        error = NULL
                    WHERE worktree_name = ?
                    """,
                    (now, now, worktree_name),
                )
                if task_id:
                    conn.execute(
                        """
                        UPDATE tasks
                        SET worktree_name = NULL,
                            updated_at = ?
                        WHERE task_id = ?
                        AND worktree_name = ?
                        """,
                        (now, task_id, worktree_name),
                    )
                self._insert_event(
                    conn,
                    worktree_name,
                    task_id,
                    "removed",
                    f"Removed worktree {worktree_name}",
                    now,
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        record = self.get_worktree(worktree_name)
        if not record:
            raise RuntimeError(f"Worktree disappeared after remove: {worktree_name}")
        return record

    def mark_failed(self, worktree_name: str, error: str) -> Optional[WorktreeRecord]:
        """Persist a failed worktree operation for later inspection."""
        try:
            return self.set_status(
                worktree_name,
                "failed",
                event_type="failed",
                message=str(error),
                error=str(error),
            )
        except ValueError:
            return None

    def set_status(
        self,
        worktree_name: str,
        status: str,
        *,
        event_type: Optional[str] = None,
        message: str = "",
        error: Optional[str] = None,
    ) -> WorktreeRecord:
        """Set one worktree status and record the event atomically."""
        self.initialize()
        if status not in WORKTREE_STATUSES:
            raise ValueError(f"invalid worktree status: {status}")
        event_name = event_type or status
        if event_name not in WORKTREE_EVENT_TYPES:
            raise ValueError(f"invalid worktree event type: {event_name}")
        now = time.time()
        timestamp_columns = {
            "approved": "approved_at",
            "committed": "committed_at",
            "merged": "merged_at",
            "kept": "kept_at",
            "removed": "removed_at",
        }
        timestamp_column = timestamp_columns.get(status)

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_worktree_row(conn, worktree_name)
                if not row:
                    raise ValueError(f"Worktree not found: {worktree_name}")
                if timestamp_column:
                    conn.execute(
                        f"""
                        UPDATE worktrees
                        SET status = ?,
                            updated_at = ?,
                            {timestamp_column} = ?,
                            error = ?
                        WHERE worktree_name = ?
                        """,
                        (status, now, now, error, worktree_name),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE worktrees
                        SET status = ?,
                            updated_at = ?,
                            error = ?
                        WHERE worktree_name = ?
                        """,
                        (status, now, error, worktree_name),
                    )
                self._insert_event(
                    conn,
                    worktree_name,
                    row["task_id"],
                    event_name,
                    message or f"Worktree {worktree_name} -> {status}",
                    now,
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        record = self.get_worktree(worktree_name)
        if not record:
            raise RuntimeError(f"Worktree disappeared after status update: {worktree_name}")
        return record

    def list_worktree_events(
        self,
        worktree_name: Optional[str] = None,
        task_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[WorktreeEventRecord]:
        """List recent worktree audit events."""
        self.initialize()
        filters = []
        params: list[object] = []
        if worktree_name:
            filters.append("worktree_name = ?")
            params.append(str(worktree_name).strip())
        if task_id:
            filters.append("task_id = ?")
            params.append(str(task_id).strip())
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(max(1, int(limit)))

        with BUS.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT event_id, worktree_name, task_id, event_type, message, created_at
                FROM worktree_events
                {where}
                ORDER BY created_at DESC, event_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            WorktreeEventRecord(
                event_id=int(row["event_id"]),
                worktree_name=str(row["worktree_name"]),
                task_id=row["task_id"],
                event_type=str(row["event_type"]),
                message=str(row["message"]),
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

    def insert_event(
        self,
        worktree_name: str,
        task_id: Optional[str],
        event_type: str,
        message: str,
    ) -> None:
        """Public helper for review/merge modules to append worktree events."""
        self.initialize()
        with BUS.connection() as conn:
            self._insert_event(conn, worktree_name, task_id, event_type, message)

    def _set_status_by_task(
        self,
        task_id: str,
        status: str,
        event_type: str,
        message: str,
        *,
        allowed_statuses: set[str],
    ) -> Optional[WorktreeRecord]:
        """Set status for a task-bound worktree inside one transaction."""
        self.initialize()
        if status not in WORKTREE_STATUSES:
            raise ValueError(f"invalid worktree status: {status}")
        if event_type not in WORKTREE_EVENT_TYPES:
            raise ValueError(f"invalid worktree event type: {event_type}")
        now = time.time()
        placeholders = ",".join("?" for _ in allowed_statuses)

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    f"""
                    SELECT worktree_name, task_id, path, branch, status, created_at,
                           updated_at, kept_at, removed_at, approved_at, committed_at,
                           merged_at, error
                    FROM worktrees
                    WHERE task_id = ?
                    AND status IN ({placeholders})
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    [str(task_id).strip(), *sorted(allowed_statuses)],
                ).fetchone()
                if not row:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    """
                    UPDATE worktrees
                    SET status = ?,
                        updated_at = ?,
                        error = NULL
                    WHERE worktree_name = ?
                    """,
                    (status, now, row["worktree_name"]),
                )
                self._insert_event(
                    conn,
                    row["worktree_name"],
                    row["task_id"],
                    event_type,
                    message,
                    now,
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        return self.get_task_worktree(task_id)

    def _validate_bindable_task(self, conn: sqlite3.Connection, task_id: str) -> None:
        """Ensure a task exists and can receive a worktree binding."""
        task_row = conn.execute(
            "SELECT task_id, status FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not task_row:
            raise ValueError(f"Task not found: {task_id}")
        if task_row["status"] != "pending":
            raise ValueError(f"Task {task_id} is {task_row['status']}, bind before claim")

    def _validate_task_has_no_other_worktree(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        worktree_name: str,
    ) -> None:
        """Reject binding one task to multiple live worktrees."""
        placeholders = ",".join("?" for _ in BOUND_WORKTREE_STATUSES)
        row = conn.execute(
            f"""
            SELECT worktree_name
            FROM worktrees
            WHERE task_id = ?
            AND worktree_name != ?
            AND status IN ({placeholders})
            LIMIT 1
            """,
            [task_id, worktree_name, *sorted(BOUND_WORKTREE_STATUSES)],
        ).fetchone()
        if row:
            raise ValueError(f"Task {task_id} already bound to worktree {row['worktree_name']}")

    def _get_worktree_row(
        self,
        conn: sqlite3.Connection,
        worktree_name: str,
    ) -> Optional[sqlite3.Row]:
        """Read one worktree row with an existing connection."""
        return conn.execute(
            """
            SELECT worktree_name, task_id, path, branch, status, created_at,
                   updated_at, kept_at, removed_at, approved_at, committed_at,
                   merged_at, error
            FROM worktrees
            WHERE worktree_name = ?
            """,
            (str(worktree_name).strip(),),
        ).fetchone()

    def _record_from_row(self, row: sqlite3.Row) -> WorktreeRecord:
        """Convert a SQLite row into a typed worktree record."""
        return WorktreeRecord(
            worktree_name=str(row["worktree_name"]),
            task_id=row["task_id"],
            path=str(row["path"]),
            branch=str(row["branch"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            kept_at=row["kept_at"],
            removed_at=row["removed_at"],
            approved_at=row["approved_at"],
            committed_at=row["committed_at"],
            merged_at=row["merged_at"],
            error=row["error"],
        )

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        worktree_name: str,
        task_id: Optional[str],
        event_type: str,
        message: str,
        created_at: Optional[float] = None,
    ) -> None:
        """Insert one audit event as part of the caller's transaction."""
        if event_type not in WORKTREE_EVENT_TYPES:
            raise ValueError(f"invalid worktree event type: {event_type}")
        conn.execute(
            """
            INSERT INTO worktree_events
            (worktree_name, task_id, event_type, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (worktree_name, task_id, event_type, str(message or ""), created_at or time.time()),
        )

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_type: str,
    ) -> None:
        """Add a nullable column for older SQLite DBs created before s18."""
        columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

    def _create_worktrees_table(self, conn: sqlite3.Connection) -> None:
        """Create the current worktrees schema."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS worktrees (
                worktree_name TEXT PRIMARY KEY,
                task_id TEXT UNIQUE,
                path TEXT NOT NULL,
                branch TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'active',
                        'ready_for_review',
                        'needs_changes',
                        'approved',
                        'committed',
                        'merged',
                        'kept',
                        'removed',
                        'failed'
                    )
                ),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                kept_at REAL,
                removed_at REAL,
                approved_at REAL,
                committed_at REAL,
                merged_at REAL,
                error TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE SET NULL
            )
            """
        )

    def _create_worktree_events_table(self, conn: sqlite3.Connection) -> None:
        """Create the current event schema without a restrictive event CHECK."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS worktree_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                worktree_name TEXT NOT NULL,
                task_id TEXT,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                FOREIGN KEY (worktree_name) REFERENCES worktrees(worktree_name),
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE SET NULL
            )
            """
        )

    def _migrate_worktrees_schema(self, conn: sqlite3.Connection) -> None:
        """Rebuild old worktrees tables whose CHECK blocks new statuses."""
        row = conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table'
            AND name = 'worktrees'
            """
        ).fetchone()
        if not row:
            return
        sql = str(row["sql"] or "")
        columns = {
            str(item["name"])
            for item in conn.execute("PRAGMA table_info(worktrees)").fetchall()
        }
        if (
            "needs_changes" in sql
            and "approved_at" in columns
            and "committed_at" in columns
            and "merged_at" in columns
        ):
            return

        conn.execute("ALTER TABLE worktrees RENAME TO worktrees_old_s18")
        self._create_worktrees_table(conn)
        conn.execute(
            """
            INSERT INTO worktrees
            (worktree_name, task_id, path, branch, status, created_at, updated_at,
             kept_at, removed_at, approved_at, committed_at, merged_at, error)
            SELECT worktree_name, task_id, path, branch, status, created_at, updated_at,
                   kept_at, removed_at, NULL, NULL, NULL, error
            FROM worktrees_old_s18
            """
        )
        conn.execute("DROP TABLE worktrees_old_s18")

    def _migrate_worktree_events_schema(self, conn: sqlite3.Connection) -> None:
        """Rebuild old event table so new event types are not blocked."""
        row = conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table'
            AND name = 'worktree_events'
            """
        ).fetchone()
        if not row:
            return
        sql = str(row["sql"] or "")
        if "CHECK" not in sql:
            return

        conn.execute("ALTER TABLE worktree_events RENAME TO worktree_events_old_s18")
        self._create_worktree_events_table(conn)
        conn.execute(
            """
            INSERT INTO worktree_events
            (event_id, worktree_name, task_id, event_type, message, created_at)
            SELECT event_id, worktree_name, task_id, event_type, message, created_at
            FROM worktree_events_old_s18
            """
        )
        conn.execute("DROP TABLE worktree_events_old_s18")

    def _rollback_quietly(self, conn: sqlite3.Connection) -> None:
        """Rollback if a transaction is active while preserving the original error."""
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass


WORKTREE_STORE = WorktreeStore()
