from __future__ import annotations

from agent.database import autonomous_tasks, worktree_reviews, worktrees
from agent.database.autonomous_tasks import AutonomousTaskStore
from agent.database.team_bus import SQLiteMessageBus
from agent.database.worktree_reviews import WorktreeReviewStore
from agent.database.worktrees import WorktreeStore
from agent.features import worktree_review
from agent.features.subagent import SUB_TOOL_NAMES
from agent.features.team import TEAM_TOOL_NAMES
from agent.tooling.permissions import check_rules


def _stores(monkeypatch, tmp_path):
    """Create task/worktree/review stores against the same temporary SQLite DB."""
    bus = SQLiteMessageBus(tmp_path / "team.sqlite3")
    monkeypatch.setattr(autonomous_tasks, "BUS", bus)
    monkeypatch.setattr(worktrees, "BUS", bus)
    monkeypatch.setattr(worktree_reviews, "BUS", bus)

    task_store = AutonomousTaskStore()
    worktree_store = WorktreeStore()
    review_store = WorktreeReviewStore()

    monkeypatch.setattr(worktrees, "TASK_STORE", task_store)
    monkeypatch.setattr(worktree_reviews, "WORKTREE_STORE", worktree_store)
    monkeypatch.setattr(worktree_review, "WORKTREE_STORE", worktree_store)
    monkeypatch.setattr(worktree_review, "WORKTREE_REVIEW_STORE", review_store)
    return task_store, worktree_store, review_store


def test_review_updates_review_row_and_worktree_status_atomically(monkeypatch, tmp_path):
    task_store, worktree_store, review_store = _stores(monkeypatch, tmp_path)
    task = task_store.create_task("review me")
    worktree_store.create_record(
        "review-a",
        path=tmp_path / ".worktrees" / "review-a",
        branch="wt/review-a",
        task_id=task.task_id,
    )
    worktree_store.mark_ready_for_review(task.task_id)

    review = review_store.record_review(
        "review-a",
        reviewer="lead",
        approve=True,
        summary="Looks good",
        notes="Ready to commit",
        diff_summary="changed app.py",
    )
    updated = worktree_store.get_worktree("review-a")
    events = worktree_store.list_worktree_events(worktree_name="review-a")

    assert review.status == "approved"
    assert updated is not None
    assert updated.status == "approved"
    assert updated.approved_at is not None
    assert any(event.event_type == "reviewed" for event in events)
    assert any(event.event_type == "approved" for event in events)


def test_merge_worktree_requires_user_confirmation_before_db_or_git(monkeypatch, tmp_path):
    _stores(monkeypatch, tmp_path)

    assert (
        worktree_review.merge_worktree("anything", user_confirmed=False)
        == "Error: merge_worktree requires explicit user confirmation"
    )


def test_permission_hook_always_gates_merge_worktree():
    assert (
        check_rules("merge_worktree", {"name": "review-a", "user_confirmed": True})
        == "Merging worktree requires explicit user confirmation"
    )


def test_teammates_and_subagents_do_not_get_review_merge_tools():
    blocked_tools = {
        "diff_worktree",
        "review_worktree",
        "test_worktree",
        "commit_worktree",
        "prepare_merge_worktree",
        "merge_worktree",
    }

    assert TEAM_TOOL_NAMES.isdisjoint(blocked_tools)
    assert SUB_TOOL_NAMES.isdisjoint(blocked_tools)
