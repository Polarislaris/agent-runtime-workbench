"""Step 4/5 tests for in-memory runs and browser permission decisions."""

from __future__ import annotations

from queue import Empty
from threading import Event
import time

import pytest

from agent.api.run_manager import (
    ActiveRunError,
    InvalidPermissionDecisionError,
    PermissionAlreadyResolvedError,
    PermissionNotFoundError,
    RunManager,
    RunNotFoundError,
)
from agent.database.runs import RunStore
from agent.runtime.events import AgentLoopResult


def wait_for_event(manager: RunManager, run_id: str, event_type: str, timeout: float = 1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = manager.snapshot(run_id)
        for event in snapshot.events:
            if event["type"] == event_type:
                return event
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {event_type}")


@pytest.fixture
def managers(tmp_path):
    created: list[RunManager] = []

    def make(**kwargs) -> RunManager:
        manager = RunManager(
            hook_collector=lambda *_args: [],
            run_store=RunStore(tmp_path / f"runs_{len(created)}.sqlite3"),
            **kwargs,
        )
        created.append(manager)
        return manager

    yield make

    for manager in created:
        manager.shutdown(wait=True)


def test_empty_prompt_is_rejected(managers):
    manager = managers(agent_runner=lambda _messages, _runtime: AgentLoopResult.completed())

    with pytest.raises(ValueError, match="prompt must not be empty"):
        manager.create_run("   ")


def test_only_one_active_run_is_allowed(managers):
    entered = Event()
    release = Event()

    def blocking_runner(_messages, _runtime):
        entered.set()
        release.wait(1)
        return AgentLoopResult.completed()

    manager = managers(agent_runner=blocking_runner)
    first = manager.create_run("first")
    assert entered.wait(1)

    try:
        with pytest.raises(ActiveRunError, match="already active"):
            manager.create_run("second")
    finally:
        release.set()

    manager.get_run(first.id).worker.result(timeout=1)


def test_live_subscription_receives_its_own_ordered_event_queue(managers):
    entered = Event()
    release = Event()

    def runner(_messages, runtime):
        entered.set()
        release.wait(1)
        runtime.events.emit("model.started", {"attempt": 1})
        return AgentLoopResult.completed()

    manager = managers(agent_runner=runner)
    created = manager.create_run("finish normally")
    state = manager.get_run(created.id)
    assert entered.wait(1)
    subscription_id, event_queue = manager.subscribe(created.id)
    release.set()
    result = state.worker.result(timeout=1)

    snapshot = manager.snapshot(state)
    queued = []
    while True:
        try:
            queued.append(event_queue.get_nowait())
        except Empty:
            break
    manager.unsubscribe(created.id, subscription_id)

    assert result.status == "completed"
    assert snapshot.status == "completed"
    assert snapshot.completed_at is not None
    assert [event["type"] for event in snapshot.events] == [
        "run.started",
        "model.started",
        "run.completed",
    ]
    assert [event.id for event in queued] == [
        event["id"] for event in snapshot.events[1:]
    ]
    assert [event.sequence for event in queued] == [2, 3]


def test_two_live_subscriptions_do_not_steal_each_others_events(managers):
    entered = Event()
    release = Event()

    def runner(_messages, runtime):
        entered.set()
        release.wait(1)
        runtime.events.emit("model.started", {"attempt": 1})
        return AgentLoopResult.completed()

    manager = managers(agent_runner=runner)
    created = manager.create_run("two browser tabs")
    assert entered.wait(1)
    first_id, first_queue = manager.subscribe(created.id)
    second_id, second_queue = manager.subscribe(created.id)
    release.set()
    manager.get_run(created.id).worker.result(timeout=1)

    first = [first_queue.get_nowait().sequence for _ in range(2)]
    second = [second_queue.get_nowait().sequence for _ in range(2)]
    manager.unsubscribe(created.id, first_id)
    manager.unsubscribe(created.id, second_id)

    assert first == [2, 3]
    assert second == [2, 3]


def test_runner_exception_becomes_failed_terminal_event(managers):
    def failing_runner(_messages, _runtime):
        raise RuntimeError("runner exploded")

    manager = managers(agent_runner=failing_runner)
    created = manager.create_run("fail")
    result = manager.get_run(created.id).worker.result(timeout=1)
    snapshot = manager.snapshot(created.id)

    assert result.status == "failed"
    assert snapshot.status == "failed"
    assert snapshot.error == "runner exploded"
    assert snapshot.events[-1]["type"] == "run.failed"


def test_cancel_sets_token_and_worker_reports_cancelled(managers):
    entered = Event()

    def cancellable_runner(_messages, runtime):
        entered.set()
        runtime.cancellation.wait(1)
        return AgentLoopResult.cancelled()

    manager = managers(agent_runner=cancellable_runner)
    created = manager.create_run("cancel me")
    assert entered.wait(1)

    manager.cancel_run(created.id)
    state = manager.get_run(created.id)
    result = state.worker.result(timeout=1)

    assert state.cancellation.is_cancelled()
    assert result.status == "cancelled"
    assert manager.snapshot(state).events[-1]["type"] == "run.cancelled"


def test_prompt_hook_messages_are_included_in_run_history(tmp_path):
    manager = RunManager(
        agent_runner=lambda _messages, _runtime: AgentLoopResult.completed(),
        hook_collector=lambda event, prompt: [
            {"role": "user", "content": f"{event}:{prompt}"}
        ],
        run_store=RunStore(tmp_path / "runs.sqlite3"),
    )
    try:
        created = manager.create_run("inspect workspace")
        manager.get_run(created.id).worker.result(timeout=1)
        messages = manager.snapshot(created.id).messages
    finally:
        manager.shutdown()

    assert messages[:2] == [
        {"role": "user", "content": "inspect workspace"},
        {
            "role": "user",
            "content": "UserPromptSubmit:inspect workspace",
        },
    ]


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_web_permission_provider_accepts_user_decision(managers, decision):
    decisions: list[str] = []

    def permission_runner(_messages, runtime):
        decisions.append(runtime.permissions.decide(
            "merge_worktree",
            {"name": "review-a", "secret": "do-not-publish"},
            "Merge requires approval",
        ))
        return AgentLoopResult.completed()

    manager = managers(agent_runner=permission_runner, permission_timeout_seconds=2)
    created = manager.create_run("merge")
    requested = wait_for_event(manager, created.id, "permission.requested")
    request_id = requested["payload"]["request_id"]

    manager.resolve_permission(created.id, request_id, decision)
    manager.get_run(created.id).worker.result(timeout=1)
    snapshot = manager.snapshot(created.id)

    assert decisions == [decision]
    assert "do-not-publish" not in str(requested)
    assert requested["payload"]["args_preview"]["secret"] == "***"
    resolved = next(
        event for event in snapshot.events
        if event["type"] == "permission.resolved"
    )
    assert resolved["payload"]["decision"] == decision
    assert resolved["payload"]["resolution"] == "user"


def test_permission_timeout_fails_closed(managers):
    decisions: list[str] = []

    def permission_runner(_messages, runtime):
        decisions.append(runtime.permissions.decide("bash", {}, "confirm"))
        return AgentLoopResult.completed()

    manager = managers(
        agent_runner=permission_runner,
        permission_timeout_seconds=0.01,
    )
    created = manager.create_run("timeout")
    manager.get_run(created.id).worker.result(timeout=1)
    snapshot = manager.snapshot(created.id)

    assert decisions == ["deny"]
    resolved = next(
        event for event in snapshot.events
        if event["type"] == "permission.resolved"
    )
    assert resolved["payload"]["resolution"] == "timeout"


def test_web_permission_is_durable_before_the_worker_is_released(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    decision_seen = Event()

    def permission_runner(_messages, runtime):
        runtime.permissions.decide("bash", {"command": "echo ok"}, "confirm")
        decision_seen.set()
        return AgentLoopResult.completed()

    manager = RunManager(
        agent_runner=permission_runner,
        hook_collector=lambda *_args: [],
        permission_timeout_seconds=2,
        run_store=store,
    )
    try:
        created = manager.create_run("durable approval")
        requested = wait_for_event(manager, created.id, "permission.requested")
        request_id = requested["payload"]["request_id"]

        assert store.get_permission_request(created.id, request_id).status == "pending"
        manager.resolve_permission(created.id, request_id, "allow")
        assert store.get_permission_request(created.id, request_id).status == "approved"
        assert decision_seen.wait(1)
        manager.get_run(created.id).worker.result(timeout=1)
    finally:
        manager.shutdown(wait=True)


def test_duplicate_and_unknown_permission_decisions_are_rejected(managers):
    release = Event()

    def permission_runner(_messages, runtime):
        runtime.permissions.decide("bash", {}, "confirm")
        release.set()
        return AgentLoopResult.completed()

    manager = managers(agent_runner=permission_runner, permission_timeout_seconds=2)
    created = manager.create_run("permission errors")
    requested = wait_for_event(manager, created.id, "permission.requested")
    request_id = requested["payload"]["request_id"]

    manager.resolve_permission(created.id, request_id, "allow")
    with pytest.raises(PermissionAlreadyResolvedError):
        manager.resolve_permission(created.id, request_id, "deny")
    with pytest.raises(PermissionNotFoundError):
        manager.resolve_permission(created.id, "perm_missing", "allow")
    with pytest.raises(RunNotFoundError):
        manager.resolve_permission("run_missing", request_id, "allow")
    with pytest.raises(InvalidPermissionDecisionError):
        manager.resolve_permission(created.id, request_id, "maybe")

    assert release.wait(1)
    manager.get_run(created.id).worker.result(timeout=1)


def test_cancel_wakes_pending_permission_without_waiting_for_timeout(managers):
    decisions: list[str] = []

    def permission_runner(_messages, runtime):
        decisions.append(runtime.permissions.decide("bash", {}, "confirm"))
        if runtime.cancellation.is_cancelled():
            return AgentLoopResult.cancelled()
        return AgentLoopResult.completed()

    manager = managers(agent_runner=permission_runner, permission_timeout_seconds=30)
    created = manager.create_run("cancel approval")
    wait_for_event(manager, created.id, "permission.requested")

    manager.cancel_run(created.id)
    manager.get_run(created.id).worker.result(timeout=1)
    snapshot = manager.snapshot(created.id)

    assert decisions == ["deny"]
    assert snapshot.status == "cancelled"
    resolved = next(
        event for event in snapshot.events
        if event["type"] == "permission.resolved"
    )
    assert resolved["payload"]["resolution"] == "cancelled"
