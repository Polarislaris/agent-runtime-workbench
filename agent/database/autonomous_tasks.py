"""SQLite task board and teammate lifecycle store for s17.

s17 lets teammate agents claim work by themselves while they are idle. That
means multiple threads may try to claim the same task at the same time, so the
task board cannot rely on read-modify-write JSON files. This module keeps the
task board in SQLite and wraps every state transition that touches multiple
tables in one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import secrets
import sqlite3
import threading
import time
from typing import Optional

from .team_bus import BUS


TASK_STATUSES = {"pending", "in_progress", "completed", "failed", "cancelled"}
AGENT_STATUSES = {"running", "idle", "shutting_down", "done", "failed"}
TASK_EVENT_TYPES = {
    "created",
    "claimed",
    "completed",
    "released",
    "failed",
    "cancelled",
    "unblocked",
}


@dataclass
class TaskRecord:
    """One task-board row plus its blockedBy dependency list."""

    task_id: str
    subject: str
    description: str
    status: str
    owner: Optional[str]
    worktree_name: Optional[str]
    priority: int
    created_at: float
    updated_at: float
    claimed_at: Optional[float]
    completed_at: Optional[float]
    failed_at: Optional[float]
    error: Optional[str]
    blockedBy: list[str]

    def to_dict(self) -> dict:
        """Return a JSON-friendly representation used by tools and prompts."""
        return {
            "id": self.task_id,
            "task_id": self.task_id,
            "subject": self.subject,
            "description": self.description,
            "status": self.status,
            "owner": self.owner,
            "worktree_name": self.worktree_name,
            "worktree": self.worktree_name,
            "priority": self.priority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "claimed_at": self.claimed_at,
            "completed_at": self.completed_at,
            "failed_at": self.failed_at,
            "error": self.error,
            "blockedBy": self.blockedBy,
        }


@dataclass
class TeamAgentRecord:
    """Durable lifecycle state for one teammate agent."""

    agent_id: str
    role: str
    status: str
    current_task_id: Optional[str]
    created_at: float
    updated_at: float
    last_seen_at: float
    idle_since: Optional[float]
    stopped_at: Optional[float]
    error: Optional[str]

    def to_dict(self) -> dict:
        """Return a plain dict for tool output and test assertions."""
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "status": self.status,
            "current_task_id": self.current_task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_seen_at": self.last_seen_at,
            "idle_since": self.idle_since,
            "stopped_at": self.stopped_at,
            "error": self.error,
        }


@dataclass
class TaskEventRecord:
    """One immutable task lifecycle audit event."""

    event_id: int
    task_id: str
    agent_id: Optional[str]
    event_type: str
    message: str
    created_at: float

    def to_dict(self) -> dict:
        """Return a JSON-friendly event representation."""
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "event_type": self.event_type,
            "message": self.message,
            "created_at": self.created_at,
        }


class AutonomousTaskStore:
    """SQLite-backed task board with atomic claim/complete transitions."""

    def __init__(self):
        self._init_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        """Create s17 tables once, sharing the same SQLite DB as team messages."""
        BUS.initialize()
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            with BUS.connection() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS team_agents (
                        agent_id TEXT PRIMARY KEY,
                        role TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('running', 'idle', 'shutting_down', 'done', 'failed')
                        ),
                        current_task_id TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        last_seen_at REAL NOT NULL,
                        idle_since REAL,
                        stopped_at REAL,
                        error TEXT,
                        FOREIGN KEY (current_task_id) REFERENCES tasks(task_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        subject TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'pending',
                                'in_progress',
                                'completed',
                                'failed',
                                'cancelled'
                            )
                        ),
                        owner TEXT,
                        worktree_name TEXT,
                        priority INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        claimed_at REAL,
                        completed_at REAL,
                        failed_at REAL,
                        error TEXT,
                        FOREIGN KEY (owner) REFERENCES team_agents(agent_id)
                    )
                    """
                )
                self._ensure_column(conn, "tasks", "worktree_name", "TEXT")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_dependencies (
                        task_id TEXT NOT NULL,
                        depends_on_task_id TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY (task_id, depends_on_task_id),
                        FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
                        FOREIGN KEY (depends_on_task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
                        CHECK (task_id != depends_on_task_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        agent_id TEXT,
                        event_type TEXT NOT NULL CHECK (
                            event_type IN (
                                'created',
                                'claimed',
                                'completed',
                                'released',
                                'failed',
                                'cancelled',
                                'unblocked'
                            )
                        ),
                        message TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
                        FOREIGN KEY (agent_id) REFERENCES team_agents(agent_id)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_team_agents_status "
                    "ON team_agents(status, updated_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_team_agents_current_task "
                    "ON team_agents(current_task_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_status_owner "
                    "ON tasks(status, owner, priority, created_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_owner "
                    "ON tasks(owner, status)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_worktree "
                    "ON tasks(worktree_name)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_updated "
                    "ON tasks(updated_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_dependencies_depends_on "
                    "ON task_dependencies(depends_on_task_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_events_task "
                    "ON task_events(task_id, created_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_events_agent "
                    "ON task_events(agent_id, created_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_events_type "
                    "ON task_events(event_type, created_at)"
                )

            self._initialized = True

    def new_task_id(self) -> str:
        """Create a compact task id compatible with the earlier s12 format."""
        for _ in range(10):
            task_id = f"task_{int(time.time())}_{secrets.token_hex(2)}"
            if not self.get_task(task_id):
                return task_id
        raise RuntimeError("Unable to allocate a unique task id")

    def register_agent(self, agent_id: str, role: str, status: str = "running") -> None:
        """Create or refresh a durable teammate lifecycle row."""
        self.initialize()
        normalized = BUS._validate_agent_id(agent_id)
        if status not in AGENT_STATUSES:
            raise ValueError(f"Invalid teammate status: {status}")
        now = time.time()
        idle_since = now if status == "idle" else None
        stopped_at = now if status in {"done", "failed"} else None

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO team_agents
                    (agent_id, role, status, current_task_id, created_at, updated_at,
                     last_seen_at, idle_since, stopped_at, error)
                    VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL)
                    """,
                    (normalized, str(role or "generalist"), status, now, now, now, idle_since, stopped_at),
                )
                conn.execute(
                    """
                    UPDATE team_agents
                    SET role = ?,
                        status = ?,
                        current_task_id = NULL,
                        updated_at = ?,
                        last_seen_at = ?,
                        idle_since = ?,
                        stopped_at = ?,
                        error = NULL
                    WHERE agent_id = ?
                    """,
                    (
                        str(role or "generalist"),
                        status,
                        now,
                        now,
                        idle_since,
                        stopped_at,
                        normalized,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

    def get_agent(self, agent_id: str) -> Optional[TeamAgentRecord]:
        """Read one durable teammate lifecycle row."""
        self.initialize()
        normalized = BUS._validate_agent_id(agent_id)
        with BUS.connection() as conn:
            row = conn.execute(
                """
                SELECT agent_id, role, status, current_task_id, created_at,
                       updated_at, last_seen_at, idle_since, stopped_at, error
                FROM team_agents
                WHERE agent_id = ?
                """,
                (normalized,),
            ).fetchone()
        if not row:
            return None
        return TeamAgentRecord(
            agent_id=str(row["agent_id"]),
            role=str(row["role"]),
            status=str(row["status"]),
            current_task_id=row["current_task_id"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_seen_at=float(row["last_seen_at"]),
            idle_since=row["idle_since"],
            stopped_at=row["stopped_at"],
            error=row["error"],
        )

    def list_agents(self) -> list[TeamAgentRecord]:
        """List durable teammate lifecycle rows for the browser inspector."""
        self.initialize()
        with BUS.connection() as conn:
            rows = conn.execute(
                """
                SELECT agent_id, role, status, current_task_id, created_at,
                       updated_at, last_seen_at, idle_since, stopped_at, error
                FROM team_agents
                ORDER BY updated_at DESC, agent_id ASC
                """
            ).fetchall()
        return [
            TeamAgentRecord(
                agent_id=str(row["agent_id"]),
                role=str(row["role"]),
                status=str(row["status"]),
                current_task_id=row["current_task_id"],
                created_at=float(row["created_at"]),
                updated_at=float(row["updated_at"]),
                last_seen_at=float(row["last_seen_at"]),
                idle_since=row["idle_since"],
                stopped_at=row["stopped_at"],
                error=row["error"],
            )
            for row in rows
        ]

    def set_agent_state(
        self,
        agent_id: str,
        status: str,
        *,
        current_task_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Update lifecycle status without changing task ownership unless asked."""
        self.initialize()
        normalized = BUS._validate_agent_id(agent_id)
        if status not in AGENT_STATUSES:
            raise ValueError(f"Invalid teammate status: {status}")
        now = time.time()
        idle_since = now if status == "idle" else None
        stopped_at = now if status in {"done", "failed"} else None

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_agent_in_connection(conn, normalized)
                if current_task_id is None:
                    conn.execute(
                        """
                        UPDATE team_agents
                        SET status = ?,
                            updated_at = ?,
                            last_seen_at = ?,
                            idle_since = ?,
                            stopped_at = COALESCE(?, stopped_at),
                            error = ?
                        WHERE agent_id = ?
                        """,
                        (status, now, now, idle_since, stopped_at, error, normalized),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE team_agents
                        SET status = ?,
                            current_task_id = ?,
                            updated_at = ?,
                            last_seen_at = ?,
                            idle_since = ?,
                            stopped_at = COALESCE(?, stopped_at),
                            error = ?
                        WHERE agent_id = ?
                        """,
                        (
                            status,
                            current_task_id,
                            now,
                            now,
                            idle_since,
                            stopped_at,
                            error,
                            normalized,
                        ),
                    )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

    def stop_agent(
        self,
        agent_id: str,
        final_status: str = "done",
        *,
        error: Optional[str] = None,
        release_task: bool = True,
    ) -> Optional[str]:
        """Mark an agent stopped and release its current task in one transaction."""
        self.initialize()
        normalized = BUS._validate_agent_id(agent_id)
        if final_status not in {"done", "failed"}:
            raise ValueError(f"Invalid final teammate status: {final_status}")
        now = time.time()
        released_task_id: Optional[str] = None

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_agent_in_connection(conn, normalized)
                row = conn.execute(
                    "SELECT current_task_id FROM team_agents WHERE agent_id = ?",
                    (normalized,),
                ).fetchone()
                current_task_id = row["current_task_id"] if row else None

                if release_task and current_task_id:
                    cursor = conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'pending',
                            owner = NULL,
                            claimed_at = NULL,
                            updated_at = ?,
                            error = NULL
                        WHERE task_id = ?
                        AND status = 'in_progress'
                        AND owner = ?
                        """,
                        (now, current_task_id, normalized),
                    )
                    if cursor.rowcount:
                        released_task_id = str(current_task_id)
                        self._insert_event(
                            conn,
                            released_task_id,
                            normalized,
                            "released",
                            error or f"{normalized} stopped before completing the task",
                            now,
                        )

                conn.execute(
                    """
                    UPDATE team_agents
                    SET status = ?,
                        current_task_id = NULL,
                        updated_at = ?,
                        last_seen_at = ?,
                        idle_since = NULL,
                        stopped_at = ?,
                        error = ?
                    WHERE agent_id = ?
                    """,
                    (final_status, now, now, now, error, normalized),
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        return released_task_id

    def create_task(
        self,
        subject: str,
        description: str = "",
        blockedBy: Optional[list[str]] = None,
        priority: int = 0,
    ) -> TaskRecord:
        """Create one pending task, its dependencies, and an audit event atomically."""
        self.initialize()
        subject = str(subject).strip()
        if not subject:
            raise ValueError("subject must not be empty")
        dependencies = self._normalize_dependency_ids(blockedBy or [])
        task_id = self.new_task_id()
        now = time.time()

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO tasks
                    (task_id, subject, description, status, owner, worktree_name, priority,
                     created_at, updated_at)
                    VALUES (?, ?, ?, 'pending', NULL, NULL, ?, ?, ?)
                    """,
                    (task_id, subject, str(description or "").strip(), int(priority), now, now),
                )
                for dep_id in dependencies:
                    conn.execute(
                        """
                        INSERT INTO task_dependencies
                        (task_id, depends_on_task_id, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (task_id, dep_id, now),
                    )
                self._insert_event(conn, task_id, None, "created", subject, now)
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        task = self.get_task(task_id)
        if not task:
            raise RuntimeError(f"Task disappeared after create: {task_id}")
        return task

    def list_tasks(
        self,
        status: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> list[TaskRecord]:
        """List tasks, optionally filtered by status or owner."""
        self.initialize()
        filters = []
        params: list[object] = []
        if status:
            normalized_status = str(status).strip()
            if normalized_status not in TASK_STATUSES:
                raise ValueError(f"invalid status: {normalized_status}")
            filters.append("status = ?")
            params.append(normalized_status)
        if owner:
            normalized_owner = BUS._validate_agent_id(owner)
            filters.append("owner = ?")
            params.append(normalized_owner)

        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with BUS.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT task_id, subject, description, status, owner, priority,
                       worktree_name, created_at, updated_at, claimed_at, completed_at,
                       failed_at, error
                FROM tasks
                {where}
                ORDER BY
                    CASE status
                        WHEN 'in_progress' THEN 0
                        WHEN 'pending' THEN 1
                        WHEN 'completed' THEN 2
                        ELSE 3
                    END,
                    priority DESC,
                    created_at ASC
                """,
                params,
            ).fetchall()
            return [self._task_from_row(conn, row) for row in rows]

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        """Read one task by id, returning None if it does not exist."""
        self.initialize()
        normalized = self._normalize_task_id(task_id)
        with BUS.connection() as conn:
            row = self._get_task_row(conn, normalized)
            return self._task_from_row(conn, row) if row else None

    def scan_unclaimed_tasks(self, limit: int = 5) -> list[TaskRecord]:
        """Return pending, unowned tasks whose dependencies are completed."""
        self.initialize()
        limit = max(1, int(limit))
        with BUS.connection() as conn:
            rows = conn.execute(
                """
                SELECT t.task_id, t.subject, t.description, t.status, t.owner,
                       t.worktree_name, t.priority, t.created_at, t.updated_at, t.claimed_at,
                       t.completed_at, t.failed_at, t.error
                FROM tasks t
                WHERE t.status = 'pending'
                AND t.owner IS NULL
                AND NOT EXISTS (
                    SELECT 1
                    FROM task_dependencies d
                    JOIN tasks dep ON dep.task_id = d.depends_on_task_id
                    WHERE d.task_id = t.task_id
                    AND dep.status != 'completed'
                )
                ORDER BY t.priority DESC, t.created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._task_from_row(conn, row) for row in rows]

    def blocked_dependency_ids(self, task_id: str) -> list[str]:
        """Return dependencies that are not completed yet."""
        self.initialize()
        normalized = self._normalize_task_id(task_id)
        with BUS.connection() as conn:
            return self._blocked_dependency_ids_in_connection(conn, normalized)

    def can_start(self, task_id: str) -> bool:
        """Return True only when all dependencies are completed."""
        return not self.blocked_dependency_ids(task_id)

    def claim_task(self, task_id: str, owner: str) -> TaskRecord:
        """Atomically claim a pending unowned task for one agent."""
        self.initialize()
        normalized_task_id = self._normalize_task_id(task_id)
        normalized_owner = BUS._validate_agent_id(owner)
        now = time.time()

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_agent_in_connection(conn, normalized_owner)
                row = self._get_task_row(conn, normalized_task_id)
                if not row:
                    raise ValueError(f"Task not found: {normalized_task_id}")
                if row["status"] != "pending":
                    raise ValueError(f"Task {normalized_task_id} is {row['status']}, cannot claim")
                if row["owner"]:
                    raise ValueError(f"Task {normalized_task_id} already owned by {row['owner']}")

                blocked = self._blocked_dependency_ids_in_connection(conn, normalized_task_id)
                if blocked:
                    raise ValueError(f"Task {normalized_task_id} is blocked by: {blocked}")

                cursor = conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'in_progress',
                        owner = ?,
                        claimed_at = ?,
                        completed_at = NULL,
                        failed_at = NULL,
                        updated_at = ?,
                        error = NULL
                    WHERE task_id = ?
                    AND status = 'pending'
                    AND owner IS NULL
                    """,
                    (normalized_owner, now, now, normalized_task_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Task {normalized_task_id} could not be claimed")

                conn.execute(
                    """
                    UPDATE team_agents
                    SET status = 'running',
                        current_task_id = ?,
                        updated_at = ?,
                        last_seen_at = ?,
                        idle_since = NULL,
                        stopped_at = NULL,
                        error = NULL
                    WHERE agent_id = ?
                    """,
                    (normalized_task_id, now, now, normalized_owner),
                )
                self._insert_event(
                    conn,
                    normalized_task_id,
                    normalized_owner,
                    "claimed",
                    f"{normalized_owner} claimed {normalized_task_id}",
                    now,
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        task = self.get_task(normalized_task_id)
        if not task:
            raise RuntimeError(f"Task disappeared after claim: {normalized_task_id}")
        return task

    def complete_task(self, task_id: str, owner: Optional[str] = None) -> tuple[TaskRecord, list[TaskRecord]]:
        """Complete an in-progress task and record newly unblocked downstream tasks."""
        self.initialize()
        normalized_task_id = self._normalize_task_id(task_id)
        normalized_owner = BUS._validate_agent_id(owner) if owner else None
        now = time.time()
        unblocked_ids: list[str] = []

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_task_row(conn, normalized_task_id)
                if not row:
                    raise ValueError(f"Task not found: {normalized_task_id}")
                if row["status"] == "completed":
                    conn.execute("COMMIT")
                    task = self._task_from_row(conn, row)
                    return task, []
                if row["status"] != "in_progress":
                    raise ValueError(
                        f"Task {normalized_task_id} is {row['status']}, claim it before completing"
                    )

                task_owner = row["owner"]
                if normalized_owner and task_owner != normalized_owner:
                    raise ValueError(
                        f"Task {normalized_task_id} is owned by {task_owner}, not {normalized_owner}"
                    )
                event_owner = str(normalized_owner or task_owner or "")
                if event_owner:
                    self._ensure_agent_in_connection(conn, event_owner)

                cursor = conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'completed',
                        completed_at = ?,
                        updated_at = ?,
                        error = NULL
                    WHERE task_id = ?
                    AND status = 'in_progress'
                    """,
                    (now, now, normalized_task_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Task {normalized_task_id} could not be completed")

                if task_owner:
                    conn.execute(
                        """
                        UPDATE team_agents
                        SET current_task_id = NULL,
                            updated_at = ?,
                            last_seen_at = ?
                        WHERE agent_id = ?
                        AND current_task_id = ?
                        """,
                        (now, now, task_owner, normalized_task_id),
                    )

                self._insert_event(
                    conn,
                    normalized_task_id,
                    event_owner or None,
                    "completed",
                    f"Completed {normalized_task_id}",
                    now,
                )

                unblocked_rows = conn.execute(
                    """
                    SELECT t.task_id
                    FROM tasks t
                    JOIN task_dependencies d ON d.task_id = t.task_id
                    WHERE d.depends_on_task_id = ?
                    AND t.status = 'pending'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM task_dependencies d2
                        JOIN tasks dep ON dep.task_id = d2.depends_on_task_id
                        WHERE d2.task_id = t.task_id
                        AND dep.status != 'completed'
                    )
                    """,
                    (normalized_task_id,),
                ).fetchall()
                unblocked_ids = [str(item["task_id"]) for item in unblocked_rows]
                for unblocked_id in unblocked_ids:
                    self._insert_event(
                        conn,
                        unblocked_id,
                        event_owner or None,
                        "unblocked",
                        f"Unblocked after {normalized_task_id} completed",
                        now,
                    )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        completed = self.get_task(normalized_task_id)
        if not completed:
            raise RuntimeError(f"Task disappeared after complete: {normalized_task_id}")
        unblocked = [
            task for task_id in unblocked_ids if (task := self.get_task(task_id)) is not None
        ]
        return completed, unblocked

    def fail_current_task(self, owner: str, error: str) -> Optional[str]:
        """Mark the owner's current task failed and clear the agent assignment."""
        self.initialize()
        normalized_owner = BUS._validate_agent_id(owner)
        now = time.time()
        failed_task_id: Optional[str] = None

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_agent_in_connection(conn, normalized_owner)
                row = conn.execute(
                    "SELECT current_task_id FROM team_agents WHERE agent_id = ?",
                    (normalized_owner,),
                ).fetchone()
                current_task_id = row["current_task_id"] if row else None
                if current_task_id:
                    cursor = conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'failed',
                            failed_at = ?,
                            updated_at = ?,
                            error = ?,
                            owner = ?
                        WHERE task_id = ?
                        AND status = 'in_progress'
                        AND owner = ?
                        """,
                        (now, now, str(error), normalized_owner, current_task_id, normalized_owner),
                    )
                    if cursor.rowcount:
                        failed_task_id = str(current_task_id)
                        self._insert_event(
                            conn,
                            failed_task_id,
                            normalized_owner,
                            "failed",
                            str(error),
                            now,
                        )

                conn.execute(
                    """
                    UPDATE team_agents
                    SET current_task_id = NULL,
                        updated_at = ?,
                        last_seen_at = ?,
                        error = ?
                    WHERE agent_id = ?
                    """,
                    (now, now, str(error), normalized_owner),
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        return failed_task_id

    def list_task_events(
        self,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[TaskEventRecord]:
        """List recent task audit events."""
        self.initialize()
        filters = []
        params: list[object] = []
        if task_id:
            filters.append("task_id = ?")
            params.append(self._normalize_task_id(task_id))
        if agent_id:
            filters.append("agent_id = ?")
            params.append(BUS._validate_agent_id(agent_id))
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        limit = max(1, int(limit))
        params.append(limit)

        with BUS.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT event_id, task_id, agent_id, event_type, message, created_at
                FROM task_events
                {where}
                ORDER BY created_at DESC, event_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            TaskEventRecord(
                event_id=int(row["event_id"]),
                task_id=str(row["task_id"]),
                agent_id=row["agent_id"],
                event_type=str(row["event_type"]),
                message=str(row["message"]),
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

    def task_count(self) -> int:
        """Return total tasks for prompt context."""
        self.initialize()
        with BUS.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()
        return int(row["count"]) if row else 0

    def _normalize_task_id(self, task_id: str) -> str:
        """Normalize task ids and reject empty strings early."""
        normalized = str(task_id).strip()
        if not normalized:
            raise ValueError("task_id must not be empty")
        return normalized

    def _normalize_dependency_ids(self, blocked_by: list[str]) -> list[str]:
        """Validate dependency ids and preserve the caller's order."""
        dependencies = []
        for dep_id in blocked_by:
            normalized = self._normalize_task_id(dep_id)
            if normalized not in dependencies:
                dependencies.append(normalized)
        return dependencies

    def _ensure_agent_in_connection(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        role: str = "manual",
    ) -> None:
        """Create a lifecycle row for manual owners such as lead or agent."""
        normalized = BUS._validate_agent_id(agent_id)
        now = time.time()
        conn.execute(
            """
            INSERT OR IGNORE INTO team_agents
            (agent_id, role, status, current_task_id, created_at, updated_at,
             last_seen_at, idle_since, stopped_at, error)
            VALUES (?, ?, 'running', NULL, ?, ?, ?, NULL, NULL, NULL)
            """,
            (normalized, role, now, now, now),
        )

    def _get_task_row(
        self,
        conn: sqlite3.Connection,
        task_id: str,
    ) -> Optional[sqlite3.Row]:
        """Read one task row within an existing connection."""
        return conn.execute(
            """
            SELECT task_id, subject, description, status, owner, priority,
                   worktree_name, created_at, updated_at, claimed_at, completed_at,
                   failed_at, error
            FROM tasks
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()

    def _task_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> TaskRecord:
        """Convert a task row plus dependency rows into a dataclass."""
        deps = conn.execute(
            """
            SELECT depends_on_task_id
            FROM task_dependencies
            WHERE task_id = ?
            ORDER BY created_at ASC, depends_on_task_id ASC
            """,
            (row["task_id"],),
        ).fetchall()
        return TaskRecord(
            task_id=str(row["task_id"]),
            subject=str(row["subject"]),
            description=str(row["description"]),
            status=str(row["status"]),
            owner=row["owner"],
            worktree_name=row["worktree_name"],
            priority=int(row["priority"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            claimed_at=row["claimed_at"],
            completed_at=row["completed_at"],
            failed_at=row["failed_at"],
            error=row["error"],
            blockedBy=[str(dep["depends_on_task_id"]) for dep in deps],
        )

    def _blocked_dependency_ids_in_connection(
        self,
        conn: sqlite3.Connection,
        task_id: str,
    ) -> list[str]:
        """Return incomplete dependencies inside an existing transaction."""
        rows = conn.execute(
            """
            SELECT d.depends_on_task_id
            FROM task_dependencies d
            JOIN tasks dep ON dep.task_id = d.depends_on_task_id
            WHERE d.task_id = ?
            AND dep.status != 'completed'
            ORDER BY d.created_at ASC, d.depends_on_task_id ASC
            """,
            (task_id,),
        ).fetchall()
        return [str(row["depends_on_task_id"]) for row in rows]

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        agent_id: Optional[str],
        event_type: str,
        message: str,
        created_at: Optional[float] = None,
    ) -> None:
        """Insert one audit event as part of the caller's transaction."""
        if event_type not in TASK_EVENT_TYPES:
            raise ValueError(f"Invalid task event type: {event_type}")
        conn.execute(
            """
            INSERT INTO task_events
            (task_id, agent_id, event_type, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, agent_id, event_type, str(message or ""), created_at or time.time()),
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

    def _rollback_quietly(self, conn: sqlite3.Connection) -> None:
        """Rollback if a transaction is active while preserving the original error."""
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass


TASK_STORE = AutonomousTaskStore()
