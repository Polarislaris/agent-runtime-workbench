"""SQLite review, check, commit, and merge records for worktree isolation.

The base worktree table answers "where is this isolated copy and what state is
it in?".  These companion tables answer "who reviewed it, what checks ran, what
commit was created, and what merge plan/result was recorded?".
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import threading
import time
from typing import Optional

from .team_bus import BUS
from .worktrees import WORKTREE_STORE


@dataclass
class WorktreeReviewRecord:
    """One human/Lead review decision for a worktree."""

    review_id: int
    worktree_name: str
    task_id: Optional[str]
    reviewer: str
    status: str
    summary: str
    notes: str
    diff_summary: str
    created_at: float

    def to_dict(self) -> dict:
        """Return JSON-friendly review data for tool output."""
        return self.__dict__.copy()


@dataclass
class WorktreeCheckRecord:
    """One test or verification command executed inside a worktree."""

    check_id: int
    worktree_name: str
    task_id: Optional[str]
    command: str
    exit_code: int
    output_preview: str
    status: str
    created_at: float

    def to_dict(self) -> dict:
        """Return JSON-friendly check data for tool output."""
        return self.__dict__.copy()


@dataclass
class WorktreeCommitRecord:
    """One Git commit created from an isolated worktree branch."""

    commit_id: int
    worktree_name: str
    task_id: Optional[str]
    branch: str
    commit_sha: str
    commit_message: str
    created_at: float

    def to_dict(self) -> dict:
        """Return JSON-friendly commit data for tool output."""
        return self.__dict__.copy()


@dataclass
class WorktreeMergeRecord:
    """One merge preparation or merge execution result."""

    merge_id: int
    worktree_name: str
    task_id: Optional[str]
    source_branch: str
    target_branch: str
    source_commit: Optional[str]
    target_before_commit: Optional[str]
    merge_commit: Optional[str]
    status: str
    plan: str
    error: Optional[str]
    user_confirmed: int
    created_at: float
    completed_at: Optional[float]

    def to_dict(self) -> dict:
        """Return JSON-friendly merge data for tool output."""
        return self.__dict__.copy()


class WorktreeReviewStore:
    """Transactional store for the second half of the worktree lifecycle."""

    def __init__(self):
        self._init_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        """Create review/check/commit/merge tables once per process."""
        WORKTREE_STORE.initialize()
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return
            with BUS.connection() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                self._create_review_tables(conn)
                self._create_indexes(conn)
            self._initialized = True

    def record_review(
        self,
        worktree_name: str,
        *,
        reviewer: str,
        approve: bool,
        summary: str = "",
        notes: str = "",
        diff_summary: str = "",
    ) -> WorktreeReviewRecord:
        """Insert a review and update worktree status in one SQLite transaction."""
        self.initialize()
        now = time.time()
        status = "approved" if approve else "needs_changes"
        reviewer_name = str(reviewer or "lead").strip() or "lead"

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._require_worktree(conn, worktree_name)
                if row["status"] not in {"ready_for_review", "needs_changes", "approved"}:
                    raise ValueError(
                        f"Worktree {worktree_name} is {row['status']}, cannot review"
                    )
                conn.execute(
                    """
                    INSERT INTO worktree_reviews
                    (worktree_name, task_id, reviewer, status, summary, notes,
                     diff_summary, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        worktree_name,
                        row["task_id"],
                        reviewer_name,
                        status,
                        str(summary or ""),
                        str(notes or ""),
                        str(diff_summary or ""),
                        now,
                    ),
                )
                review_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                approved_at_sql = "approved_at = ?," if approve else ""
                params: list[object] = [status, now]
                if approve:
                    params.append(now)
                params.append(worktree_name)
                conn.execute(
                    f"""
                    UPDATE worktrees
                    SET status = ?,
                        updated_at = ?,
                        {approved_at_sql}
                        error = NULL
                    WHERE worktree_name = ?
                    """,
                    params,
                )
                self._insert_event(
                    conn,
                    worktree_name,
                    row["task_id"],
                    "reviewed",
                    str(summary or f"Review result: {status}"),
                    now,
                )
                self._insert_event(
                    conn,
                    worktree_name,
                    row["task_id"],
                    status,
                    str(notes or f"Worktree {worktree_name} marked {status}"),
                    now,
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        record = self._get_review(review_id)
        if not record:
            raise RuntimeError(f"Review disappeared after insert: {review_id}")
        return record

    def record_check(
        self,
        worktree_name: str,
        *,
        command: str,
        exit_code: int,
        output_preview: str,
    ) -> WorktreeCheckRecord:
        """Persist one test/check result and its audit event atomically."""
        self.initialize()
        now = time.time()
        status = "passed" if int(exit_code) == 0 else "failed"

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._require_worktree(conn, worktree_name)
                conn.execute(
                    """
                    INSERT INTO worktree_checks
                    (worktree_name, task_id, command, exit_code, output_preview,
                     status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        worktree_name,
                        row["task_id"],
                        str(command),
                        int(exit_code),
                        str(output_preview or ""),
                        status,
                        now,
                    ),
                )
                check_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                self._insert_event(
                    conn,
                    worktree_name,
                    row["task_id"],
                    "checked",
                    f"Check {status}: {command}",
                    now,
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        record = self._get_check(check_id)
        if not record:
            raise RuntimeError(f"Check disappeared after insert: {check_id}")
        return record

    def record_commit(
        self,
        worktree_name: str,
        *,
        commit_sha: str,
        commit_message: str,
    ) -> WorktreeCommitRecord:
        """Record a Git commit and move the worktree to committed atomically."""
        self.initialize()
        now = time.time()

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._require_worktree(conn, worktree_name)
                conn.execute(
                    """
                    INSERT INTO worktree_commits
                    (worktree_name, task_id, branch, commit_sha, commit_message, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        worktree_name,
                        row["task_id"],
                        row["branch"],
                        str(commit_sha),
                        str(commit_message),
                        now,
                    ),
                )
                commit_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    UPDATE worktrees
                    SET status = 'committed',
                        committed_at = ?,
                        updated_at = ?,
                        error = NULL
                    WHERE worktree_name = ?
                    """,
                    (now, now, worktree_name),
                )
                self._insert_event(
                    conn,
                    worktree_name,
                    row["task_id"],
                    "committed",
                    f"Committed {commit_sha}",
                    now,
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        record = self._get_commit(commit_id)
        if not record:
            raise RuntimeError(f"Commit disappeared after insert: {commit_id}")
        return record

    def prepare_merge(
        self,
        worktree_name: str,
        *,
        target_branch: str,
        source_commit: str,
        target_before_commit: str,
        plan: str,
    ) -> WorktreeMergeRecord:
        """Persist a merge plan without changing Git branches."""
        self.initialize()
        now = time.time()

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._require_worktree(conn, worktree_name)
                conn.execute(
                    """
                    INSERT INTO worktree_merges
                    (worktree_name, task_id, source_branch, target_branch,
                     source_commit, target_before_commit, merge_commit, status,
                     plan, error, user_confirmed, created_at, completed_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, 'prepared', ?, NULL, 0, ?, NULL)
                    """,
                    (
                        worktree_name,
                        row["task_id"],
                        row["branch"],
                        str(target_branch),
                        str(source_commit),
                        str(target_before_commit),
                        str(plan or ""),
                        now,
                    ),
                )
                merge_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                self._insert_event(
                    conn,
                    worktree_name,
                    row["task_id"],
                    "merge_prepared",
                    str(plan or f"Prepared merge into {target_branch}"),
                    now,
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

        record = self._get_merge(merge_id)
        if not record:
            raise RuntimeError(f"Merge plan disappeared after insert: {merge_id}")
        return record

    def record_merge_success(
        self,
        worktree_name: str,
        *,
        target_branch: str,
        merge_commit: str,
    ) -> WorktreeMergeRecord:
        """Mark the latest prepared merge as merged and update worktree status."""
        self.initialize()
        now = time.time()

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._require_worktree(conn, worktree_name)
                merge_row = self._latest_merge(conn, worktree_name, target_branch)
                if not merge_row:
                    raise ValueError(
                        f"No prepared merge found for {worktree_name} -> {target_branch}"
                    )
                conn.execute(
                    """
                    UPDATE worktree_merges
                    SET status = 'merged',
                        merge_commit = ?,
                        user_confirmed = 1,
                        completed_at = ?,
                        error = NULL
                    WHERE merge_id = ?
                    """,
                    (str(merge_commit), now, merge_row["merge_id"]),
                )
                conn.execute(
                    """
                    UPDATE worktrees
                    SET status = 'merged',
                        merged_at = ?,
                        updated_at = ?,
                        error = NULL
                    WHERE worktree_name = ?
                    """,
                    (now, now, worktree_name),
                )
                self._insert_event(
                    conn,
                    worktree_name,
                    row["task_id"],
                    "merged",
                    f"Merged into {target_branch}: {merge_commit}",
                    now,
                )
                conn.execute("COMMIT")
                merge_id = int(merge_row["merge_id"])
            except Exception:
                self._rollback_quietly(conn)
                raise

        record = self._get_merge(merge_id)
        if not record:
            raise RuntimeError(f"Merge record disappeared after update: {merge_id}")
        return record

    def record_merge_failure(
        self,
        worktree_name: str,
        *,
        target_branch: str,
        error: str,
    ) -> None:
        """Record a failed merge attempt without hiding the previous worktree status."""
        self.initialize()
        now = time.time()

        with BUS.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._require_worktree(conn, worktree_name)
                merge_row = self._latest_merge(conn, worktree_name, target_branch)
                if merge_row:
                    conn.execute(
                        """
                        UPDATE worktree_merges
                        SET status = 'failed',
                            error = ?,
                            completed_at = ?
                        WHERE merge_id = ?
                        """,
                        (str(error), now, merge_row["merge_id"]),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO worktree_merges
                        (worktree_name, task_id, source_branch, target_branch,
                         source_commit, target_before_commit, merge_commit, status,
                         plan, error, user_confirmed, created_at, completed_at)
                        VALUES (?, ?, ?, ?, NULL, NULL, NULL, 'failed', '', ?, 1, ?, ?)
                        """,
                        (
                            worktree_name,
                            row["task_id"],
                            row["branch"],
                            str(target_branch),
                            str(error),
                            now,
                            now,
                        ),
                    )
                self._insert_event(
                    conn,
                    worktree_name,
                    row["task_id"],
                    "merge_failed",
                    str(error),
                    now,
                )
                conn.execute("COMMIT")
            except Exception:
                self._rollback_quietly(conn)
                raise

    def list_reviews(self, worktree_name: str = "", limit: int = 20) -> list[WorktreeReviewRecord]:
        """List recent review records, optionally filtered by worktree."""
        self.initialize()
        where, params = self._optional_worktree_filter(worktree_name)
        params.append(max(1, int(limit)))
        with BUS.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT review_id, worktree_name, task_id, reviewer, status, summary,
                       notes, diff_summary, created_at
                FROM worktree_reviews
                {where}
                ORDER BY created_at DESC, review_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._review_from_row(row) for row in rows]

    def list_checks(self, worktree_name: str = "", limit: int = 20) -> list[WorktreeCheckRecord]:
        """List recent check records, optionally filtered by worktree."""
        self.initialize()
        where, params = self._optional_worktree_filter(worktree_name)
        params.append(max(1, int(limit)))
        with BUS.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT check_id, worktree_name, task_id, command, exit_code,
                       output_preview, status, created_at
                FROM worktree_checks
                {where}
                ORDER BY created_at DESC, check_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._check_from_row(row) for row in rows]

    def list_merges(self, worktree_name: str = "", limit: int = 20) -> list[WorktreeMergeRecord]:
        """List recent merge records, optionally filtered by worktree."""
        self.initialize()
        where, params = self._optional_worktree_filter(worktree_name)
        params.append(max(1, int(limit)))
        with BUS.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT merge_id, worktree_name, task_id, source_branch, target_branch,
                       source_commit, target_before_commit, merge_commit, status,
                       plan, error, user_confirmed, created_at, completed_at
                FROM worktree_merges
                {where}
                ORDER BY created_at DESC, merge_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._merge_from_row(row) for row in rows]

    def _create_review_tables(self, conn: sqlite3.Connection) -> None:
        """Create all s18 review/merge companion tables."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS worktree_reviews (
                review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                worktree_name TEXT NOT NULL,
                task_id TEXT,
                reviewer TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('approved', 'needs_changes')),
                summary TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                diff_summary TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                FOREIGN KEY (worktree_name) REFERENCES worktrees(worktree_name),
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS worktree_commits (
                commit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                worktree_name TEXT NOT NULL,
                task_id TEXT,
                branch TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                commit_message TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (worktree_name) REFERENCES worktrees(worktree_name),
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS worktree_merges (
                merge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                worktree_name TEXT NOT NULL,
                task_id TEXT,
                source_branch TEXT NOT NULL,
                target_branch TEXT NOT NULL,
                source_commit TEXT,
                target_before_commit TEXT,
                merge_commit TEXT,
                status TEXT NOT NULL CHECK (status IN ('prepared', 'merged', 'failed', 'aborted')),
                plan TEXT NOT NULL DEFAULT '',
                error TEXT,
                user_confirmed INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                completed_at REAL,
                FOREIGN KEY (worktree_name) REFERENCES worktrees(worktree_name),
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS worktree_checks (
                check_id INTEGER PRIMARY KEY AUTOINCREMENT,
                worktree_name TEXT NOT NULL,
                task_id TEXT,
                command TEXT NOT NULL,
                exit_code INTEGER NOT NULL,
                output_preview TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
                created_at REAL NOT NULL,
                FOREIGN KEY (worktree_name) REFERENCES worktrees(worktree_name),
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE SET NULL
            )
            """
        )

    def _create_indexes(self, conn: sqlite3.Connection) -> None:
        """Create lookup indexes used by review and dashboard flows."""
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_worktree_reviews_worktree "
            "ON worktree_reviews(worktree_name, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_worktree_reviews_status "
            "ON worktree_reviews(status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_worktree_commits_worktree "
            "ON worktree_commits(worktree_name, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_worktree_commits_sha "
            "ON worktree_commits(commit_sha)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_worktree_merges_worktree "
            "ON worktree_merges(worktree_name, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_worktree_merges_status "
            "ON worktree_merges(status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_worktree_checks_worktree "
            "ON worktree_checks(worktree_name, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_worktree_checks_status "
            "ON worktree_checks(status, created_at)"
        )

    def _require_worktree(self, conn: sqlite3.Connection, worktree_name: str) -> sqlite3.Row:
        """Read one worktree row inside a transaction or fail clearly."""
        row = conn.execute(
            """
            SELECT worktree_name, task_id, path, branch, status
            FROM worktrees
            WHERE worktree_name = ?
            """,
            (worktree_name,),
        ).fetchone()
        if not row:
            raise ValueError(f"Worktree not found: {worktree_name}")
        return row

    def _latest_merge(
        self,
        conn: sqlite3.Connection,
        worktree_name: str,
        target_branch: str,
    ) -> Optional[sqlite3.Row]:
        """Return the newest prepared merge for this worktree and target branch."""
        return conn.execute(
            """
            SELECT merge_id
            FROM worktree_merges
            WHERE worktree_name = ?
            AND target_branch = ?
            AND status = 'prepared'
            ORDER BY created_at DESC, merge_id DESC
            LIMIT 1
            """,
            (worktree_name, target_branch),
        ).fetchone()

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        worktree_name: str,
        task_id: Optional[str],
        event_type: str,
        message: str,
        created_at: Optional[float] = None,
    ) -> None:
        """Append an audit event within the caller's transaction."""
        conn.execute(
            """
            INSERT INTO worktree_events
            (worktree_name, task_id, event_type, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (worktree_name, task_id, event_type, str(message or ""), created_at or time.time()),
        )

    def _get_review(self, review_id: int) -> Optional[WorktreeReviewRecord]:
        """Load one review by primary key."""
        with BUS.connection() as conn:
            row = conn.execute(
                """
                SELECT review_id, worktree_name, task_id, reviewer, status, summary,
                       notes, diff_summary, created_at
                FROM worktree_reviews
                WHERE review_id = ?
                """,
                (review_id,),
            ).fetchone()
        return self._review_from_row(row) if row else None

    def _get_check(self, check_id: int) -> Optional[WorktreeCheckRecord]:
        """Load one check by primary key."""
        with BUS.connection() as conn:
            row = conn.execute(
                """
                SELECT check_id, worktree_name, task_id, command, exit_code,
                       output_preview, status, created_at
                FROM worktree_checks
                WHERE check_id = ?
                """,
                (check_id,),
            ).fetchone()
        return self._check_from_row(row) if row else None

    def _get_commit(self, commit_id: int) -> Optional[WorktreeCommitRecord]:
        """Load one commit by primary key."""
        with BUS.connection() as conn:
            row = conn.execute(
                """
                SELECT commit_id, worktree_name, task_id, branch, commit_sha,
                       commit_message, created_at
                FROM worktree_commits
                WHERE commit_id = ?
                """,
                (commit_id,),
            ).fetchone()
        return self._commit_from_row(row) if row else None

    def _get_merge(self, merge_id: int) -> Optional[WorktreeMergeRecord]:
        """Load one merge record by primary key."""
        with BUS.connection() as conn:
            row = conn.execute(
                """
                SELECT merge_id, worktree_name, task_id, source_branch, target_branch,
                       source_commit, target_before_commit, merge_commit, status,
                       plan, error, user_confirmed, created_at, completed_at
                FROM worktree_merges
                WHERE merge_id = ?
                """,
                (merge_id,),
            ).fetchone()
        return self._merge_from_row(row) if row else None

    def _optional_worktree_filter(self, worktree_name: str) -> tuple[str, list[object]]:
        """Build an optional WHERE clause for list helpers."""
        normalized = str(worktree_name or "").strip()
        if not normalized:
            return "", []
        return "WHERE worktree_name = ?", [normalized]

    def _review_from_row(self, row: sqlite3.Row) -> WorktreeReviewRecord:
        """Convert a SQLite row to a review dataclass."""
        return WorktreeReviewRecord(
            review_id=int(row["review_id"]),
            worktree_name=str(row["worktree_name"]),
            task_id=row["task_id"],
            reviewer=str(row["reviewer"]),
            status=str(row["status"]),
            summary=str(row["summary"]),
            notes=str(row["notes"]),
            diff_summary=str(row["diff_summary"]),
            created_at=float(row["created_at"]),
        )

    def _check_from_row(self, row: sqlite3.Row) -> WorktreeCheckRecord:
        """Convert a SQLite row to a check dataclass."""
        return WorktreeCheckRecord(
            check_id=int(row["check_id"]),
            worktree_name=str(row["worktree_name"]),
            task_id=row["task_id"],
            command=str(row["command"]),
            exit_code=int(row["exit_code"]),
            output_preview=str(row["output_preview"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
        )

    def _commit_from_row(self, row: sqlite3.Row) -> WorktreeCommitRecord:
        """Convert a SQLite row to a commit dataclass."""
        return WorktreeCommitRecord(
            commit_id=int(row["commit_id"]),
            worktree_name=str(row["worktree_name"]),
            task_id=row["task_id"],
            branch=str(row["branch"]),
            commit_sha=str(row["commit_sha"]),
            commit_message=str(row["commit_message"]),
            created_at=float(row["created_at"]),
        )

    def _merge_from_row(self, row: sqlite3.Row) -> WorktreeMergeRecord:
        """Convert a SQLite row to a merge dataclass."""
        return WorktreeMergeRecord(
            merge_id=int(row["merge_id"]),
            worktree_name=str(row["worktree_name"]),
            task_id=row["task_id"],
            source_branch=str(row["source_branch"]),
            target_branch=str(row["target_branch"]),
            source_commit=row["source_commit"],
            target_before_commit=row["target_before_commit"],
            merge_commit=row["merge_commit"],
            status=str(row["status"]),
            plan=str(row["plan"]),
            error=row["error"],
            user_confirmed=int(row["user_confirmed"]),
            created_at=float(row["created_at"]),
            completed_at=row["completed_at"],
        )

    def _rollback_quietly(self, conn: sqlite3.Connection) -> None:
        """Rollback without masking the original exception."""
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass


WORKTREE_REVIEW_STORE = WorktreeReviewStore()
