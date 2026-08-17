"""Stable-version S1 tests for durable Web run storage."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.database.runs import (
    EventSequenceError,
    PermissionAlreadyResolvedStoreError,
    RunStore,
    RunStoreError,
    SCHEMA_VERSION,
)
from agent.runtime.events import RunEvent


FIXED_TIME = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)


def make_event(
    sequence: int,
    *,
    event_id: str | None = None,
    event_type: str = "tool.completed",
) -> RunEvent:
    """Build deterministic persisted events without involving a live sink."""
    return RunEvent.create(
        run_id="run_001",
        sequence=sequence,
        event_type=event_type,
        payload={"tool": "read_file", "sequence": sequence},
        clock=lambda: FIXED_TIME,
        event_id_factory=lambda: event_id or f"evt_{sequence:03d}",
    )


def test_initialize_is_idempotent_and_sets_sqlite_pragmas(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")

    store.initialize()
    store.initialize()

    with store.connection() as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert version == SCHEMA_VERSION
    assert journal_mode == "wal"
    assert {"runs", "run_messages", "run_events", "permission_requests"} <= tables


def test_run_messages_events_and_status_survive_a_new_store_instance(tmp_path):
    db_path = tmp_path / "runs.sqlite3"
    store = RunStore(db_path)
    created = store.create_run(
        "run_001",
        "Inspect login tests",
        started_at="2026-08-17T08:00:00Z",
    )
    store.append_message(
        created.id,
        "user",
        "Inspect login tests",
        created_at="2026-08-17T08:00:01Z",
    )
    store.append_message(
        created.id,
        "assistant",
        [{"type": "tool_use", "id": "toolu_1", "name": "read_file"}],
        created_at="2026-08-17T08:00:02Z",
    )
    store.append_event(make_event(1, event_type="run.started"))
    store.append_event(make_event(2))
    store.update_run_status(
        created.id,
        "completed",
        completed_at="2026-08-17T08:00:03Z",
    )

    reopened = RunStore(db_path)
    restored = reopened.get_run("run_001")
    messages = reopened.list_messages("run_001")
    events = reopened.list_events("run_001")

    assert restored.status == "completed"
    assert restored.completed_at == "2026-08-17T08:00:03Z"
    assert restored.last_sequence == 2
    assert [(message.role, message.content) for message in messages] == [
        ("user", "Inspect login tests"),
        ("assistant", [{"type": "tool_use", "id": "toolu_1", "name": "read_file"}]),
    ]
    assert [event.sequence for event in events] == [1, 2]
    assert all(event.schema_version == 1 for event in events)


def test_event_sequence_failure_rolls_back_without_advancing_run_metadata(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    store.create_run("run_001", "Sequence test")

    with pytest.raises(EventSequenceError, match="expected event sequence 1"):
        store.append_event(make_event(2))

    assert store.get_run("run_001").last_sequence == 0
    assert store.list_events("run_001") == []

    store.append_event(make_event(1))
    with pytest.raises(RunStoreError, match="duplicate durable event id"):
        store.append_event(make_event(2, event_id="evt_001"))

    # The failed INSERT and metadata update share one transaction. Neither part
    # of the duplicate append is visible afterwards.
    assert store.get_run("run_001").last_sequence == 1
    assert [event.id for event in store.list_events("run_001")] == ["evt_001"]


def test_append_next_event_allocates_sequence_and_updates_status_atomically(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    store.create_run("run_001", "Durable event sink")

    event = store.append_next_event(
        "run_001",
        "run.started",
        {"status": "running"},
        event_id_factory=lambda: "evt_started",
        created_at="2026-08-17T08:00:00Z",
    )

    assert event.sequence == 1
    assert store.get_run("run_001").status == "running"
    assert store.get_run("run_001").last_sequence == 1
    assert [stored.id for stored in store.list_events("run_001")] == ["evt_started"]


def test_permission_records_are_auditable_and_resolve_once(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    store.create_run("run_001", "Review worktree")
    pending = store.create_permission_request(
        "perm_001",
        "run_001",
        "merge_worktree",
        {"name": "review-a"},
        "Merge requires approval",
        created_at="2026-08-17T08:00:00Z",
    )

    assert pending.status == "pending"
    resolved = store.resolve_permission_request(
        "run_001",
        "perm_001",
        "allow",
        resolved_at="2026-08-17T08:00:01Z",
    )
    assert resolved.status == "approved"
    assert resolved.decision == "allow"
    assert resolved.resolved_at == "2026-08-17T08:00:01Z"

    with pytest.raises(PermissionAlreadyResolvedStoreError):
        store.resolve_permission_request("run_001", "perm_001", "deny")
