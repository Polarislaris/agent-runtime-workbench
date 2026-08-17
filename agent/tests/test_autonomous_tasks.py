from __future__ import annotations

import pytest

from agent.database import autonomous_tasks
from agent.database.autonomous_tasks import AutonomousTaskStore
from agent.database.team_bus import SQLiteMessageBus


def _store(monkeypatch, tmp_path) -> AutonomousTaskStore:
    """Create an isolated SQLite task store for each test."""
    bus = SQLiteMessageBus(tmp_path / "team.sqlite3")
    monkeypatch.setattr(autonomous_tasks, "BUS", bus)
    store = AutonomousTaskStore()
    return store


def test_claim_is_blocked_until_dependencies_complete(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    schema = store.create_task("setup schema")
    api = store.create_task("write api", blockedBy=[schema.task_id])

    with pytest.raises(ValueError, match="blocked by"):
        store.claim_task(api.task_id, owner="alice")

    store.claim_task(schema.task_id, owner="alice")
    completed, unblocked = store.complete_task(schema.task_id, owner="alice")

    assert completed.status == "completed"
    assert [task.task_id for task in unblocked] == [api.task_id]
    assert store.claim_task(api.task_id, owner="bob").owner == "bob"


def test_only_one_owner_can_claim_task(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    task = store.create_task("shared work")

    claimed = store.claim_task(task.task_id, owner="alice")

    assert claimed.owner == "alice"
    with pytest.raises(ValueError, match="in_progress"):
        store.claim_task(task.task_id, owner="bob")


def test_stop_agent_releases_current_task(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.register_agent("alice", "builder")
    task = store.create_task("unfinished work")
    store.claim_task(task.task_id, owner="alice")

    released_task_id = store.stop_agent("alice", final_status="done", release_task=True)
    released = store.get_task(task.task_id)
    agent = store.get_agent("alice")

    assert released_task_id == task.task_id
    assert released is not None
    assert released.status == "pending"
    assert released.owner is None
    assert agent is not None
    assert agent.status == "done"
    assert agent.current_task_id is None
