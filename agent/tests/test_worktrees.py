from __future__ import annotations

import pytest

from agent.database import autonomous_tasks, worktrees
from agent.database.autonomous_tasks import AutonomousTaskStore
from agent.database.team_bus import SQLiteMessageBus
from agent.database.worktrees import WorktreeStore
from agent.features.worktree import validate_worktree_name


def _stores(monkeypatch, tmp_path):
    """Create isolated task/worktree stores backed by one temp SQLite DB."""
    bus = SQLiteMessageBus(tmp_path / "team.sqlite3")
    monkeypatch.setattr(autonomous_tasks, "BUS", bus)
    monkeypatch.setattr(worktrees, "BUS", bus)
    task_store = AutonomousTaskStore()
    monkeypatch.setattr(worktrees, "TASK_STORE", task_store)
    worktree_store = WorktreeStore()
    return task_store, worktree_store


def test_worktree_name_validation_rejects_path_traversal():
    assert validate_worktree_name("task_a-1.2") == "task_a-1.2"
    with pytest.raises(ValueError):
        validate_worktree_name("../escape")


def test_bind_task_to_worktree_updates_task_and_worktree_atomically(monkeypatch, tmp_path):
    task_store, worktree_store = _stores(monkeypatch, tmp_path)
    task = task_store.create_task("isolated task")
    worktree_store.create_record(
        "task-a",
        path=tmp_path / ".worktrees" / "task-a",
        branch="wt/task-a",
    )

    record = worktree_store.bind_task(task.task_id, "task-a")
    updated_task = task_store.get_task(task.task_id)
    events = worktree_store.list_worktree_events(worktree_name="task-a")

    assert record.task_id == task.task_id
    assert updated_task is not None
    assert updated_task.worktree_name == "task-a"
    assert any(event.event_type == "bound" for event in events)
