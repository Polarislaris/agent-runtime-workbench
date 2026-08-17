"""SQLite-backed message bus for s15 Agent teams.

The teaching s15 chapter uses `.mailboxes/*.jsonl` files. This module keeps the
same mailbox idea but stores every message in SQLite so concurrent teammate
threads can claim and acknowledge messages with clear transaction boundaries.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterator, Iterable, Optional

from ..config import TEAM_DB, TEAM_INBOX_LIMIT


AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass
class Message:
    """One durable team message read from the SQLite bus."""

    id: int
    from_agent: str
    to_agent: str
    type: str
    content: str
    metadata: dict
    created_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Message":
        """Convert a SQLite row into a JSON-friendly dataclass."""
        metadata_text = row["metadata_json"] or "{}"
        try:
            metadata = json.loads(metadata_text)
        except json.JSONDecodeError:
            # Keep broken metadata visible without failing the whole inbox read.
            metadata = {"_raw": metadata_text}

        return cls(
            id=int(row["id"]),
            from_agent=str(row["from_agent"]),
            to_agent=str(row["to_agent"]),
            type=str(row["type"]),
            content=str(row["content"]),
            metadata=metadata,
            created_at=float(row["created_at"]),
        )

    def to_dict(self) -> dict:
        """Return a plain dict for tests, debug output, and prompt formatting."""
        return {
            "id": self.id,
            "from": self.from_agent,
            "to": self.to_agent,
            "type": self.type,
            "content": self.content,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


class SQLiteMessageBus:
    """Durable message bus with claim/ack inbox semantics.

    Each operation opens its own SQLite connection. Connections are intentionally
    not shared across threads because Python's sqlite objects are thread-bound by
    default and short-lived connections are plenty fast for this small project.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or TEAM_DB)
        self._init_lock = threading.Lock()
        self._initialized = False

    def connect(self) -> sqlite3.Connection:
        """Open one configured SQLite connection for the current operation."""
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
        """Yield a connection and always close it after the operation."""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create schema once, guarded so concurrent first callers stay orderly."""
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            with self.connection() as conn:
                # WAL lets readers continue while another thread briefly writes.
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        from_agent TEXT NOT NULL,
                        to_agent TEXT NOT NULL,
                        type TEXT NOT NULL DEFAULT 'message',
                        content TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL,
                        claimed_at REAL,
                        consumed_at REAL,
                        failed_at REAL,
                        error TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_messages_inbox
                    ON messages(to_agent, consumed_at, claimed_at, id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_messages_created
                    ON messages(created_at)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_messages_type
                    ON messages(type)
                    """
                )

            self._initialized = True

    def _validate_agent_id(self, agent_id: str) -> str:
        """Reject odd agent names before they enter prompts or future paths."""
        normalized = str(agent_id).strip()
        if not AGENT_ID_PATTERN.match(normalized):
            raise ValueError(
                "agent id must be 1-64 chars: letters, digits, underscore, or dash"
            )
        return normalized

    def _message_placeholders(self, message_ids: Iterable[int]) -> tuple[str, list[int]]:
        """Build a parameterized IN-list for message id updates."""
        ids = [int(message_id) for message_id in message_ids]
        if not ids:
            return "", []
        return ",".join("?" for _ in ids), ids

    def send(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        msg_type: str = "message",
        metadata: Optional[dict] = None,
    ) -> Message:
        """Persist one message and return the stored message with its id."""
        self.initialize()
        sender = self._validate_agent_id(from_agent)
        recipient = self._validate_agent_id(to_agent)
        message_type = str(msg_type or "message").strip() or "message"
        message_content = str(content)
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        created_at = time.time()

        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages
                (from_agent, to_agent, type, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sender,
                    recipient,
                    message_type,
                    message_content,
                    metadata_json,
                    created_at,
                ),
            )
            message_id = int(cursor.lastrowid)

        return Message(
            id=message_id,
            from_agent=sender,
            to_agent=recipient,
            type=message_type,
            content=message_content,
            metadata=metadata or {},
            created_at=created_at,
        )

    def claim_inbox(self, agent: str, limit: int = TEAM_INBOX_LIMIT) -> list[Message]:
        """Atomically claim unread messages for one agent.

        Claim and ack are intentionally separate. A message is only acked after
        the caller has injected it into that agent's conversation history, so a
        crash between claim and injection can be recovered by releasing stale
        claims instead of silently losing the message.
        """
        self.initialize()
        recipient = self._validate_agent_id(agent)
        limit = max(1, int(limit))
        claimed_at = time.time()

        with self.connection() as conn:
            try:
                # BEGIN IMMEDIATE takes the write lock before SELECT so another
                # thread cannot claim the same rows between our SELECT and UPDATE.
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """
                    SELECT id, from_agent, to_agent, type, content, metadata_json, created_at
                    FROM messages
                    WHERE to_agent = ?
                    AND consumed_at IS NULL
                    AND claimed_at IS NULL
                    AND failed_at IS NULL
                    ORDER BY id
                    LIMIT ?
                    """,
                    (recipient, limit),
                ).fetchall()

                ids = [int(row["id"]) for row in rows]
                if ids:
                    placeholders, params = self._message_placeholders(ids)
                    conn.execute(
                        f"UPDATE messages SET claimed_at = ? WHERE id IN ({placeholders})",
                        [claimed_at, *params],
                    )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    # If BEGIN itself failed, there may be no active transaction
                    # to roll back. Preserve the original exception.
                    pass
                raise

        return [Message.from_row(row) for row in rows]

    def ack_messages(self, message_ids: list[int]) -> int:
        """Mark messages consumed after they have entered agent history."""
        self.initialize()
        placeholders, ids = self._message_placeholders(message_ids)
        if not ids:
            return 0

        consumed_at = time.time()
        with self.connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE messages
                SET consumed_at = ?
                WHERE id IN ({placeholders})
                AND consumed_at IS NULL
                """,
                [consumed_at, *ids],
            )
            return int(cursor.rowcount)

    def release_messages(self, message_ids: list[int]) -> int:
        """Release claimed messages so they can be read again."""
        self.initialize()
        placeholders, ids = self._message_placeholders(message_ids)
        if not ids:
            return 0

        with self.connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE messages
                SET claimed_at = NULL
                WHERE id IN ({placeholders})
                AND consumed_at IS NULL
                """,
                ids,
            )
            return int(cursor.rowcount)

    def fail_messages(self, message_ids: list[int], error: str) -> int:
        """Record a failed consumption attempt while preserving audit history."""
        self.initialize()
        placeholders, ids = self._message_placeholders(message_ids)
        if not ids:
            return 0

        failed_at = time.time()
        with self.connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE messages
                SET failed_at = ?, error = ?, claimed_at = NULL
                WHERE id IN ({placeholders})
                AND consumed_at IS NULL
                """,
                [failed_at, str(error), *ids],
            )
            return int(cursor.rowcount)

    def peek_inbox(self, agent: str, limit: int = TEAM_INBOX_LIMIT) -> list[Message]:
        """Inspect pending inbox messages without claiming them."""
        self.initialize()
        recipient = self._validate_agent_id(agent)
        limit = max(1, int(limit))
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, from_agent, to_agent, type, content, metadata_json, created_at
                FROM messages
                WHERE to_agent = ?
                AND consumed_at IS NULL
                AND failed_at IS NULL
                ORDER BY id
                LIMIT ?
                """,
                (recipient, limit),
            ).fetchall()
        return [Message.from_row(row) for row in rows]

    def release_stale_claims(self, max_age_seconds: int = 300) -> int:
        """Recover messages claimed by an interrupted teammate or Lead turn."""
        self.initialize()
        cutoff = time.time() - int(max_age_seconds)
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE messages
                SET claimed_at = NULL
                WHERE consumed_at IS NULL
                AND claimed_at IS NOT NULL
                AND claimed_at < ?
                """,
                (cutoff,),
            )
            return int(cursor.rowcount)


BUS = SQLiteMessageBus()
