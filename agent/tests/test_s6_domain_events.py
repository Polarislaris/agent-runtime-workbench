"""S6 coverage: domain events are emitted from real feature state changes."""

from __future__ import annotations

import importlib
import json
import time
from types import SimpleNamespace

from agent.database import autonomous_tasks
from agent.database.team_bus import SQLiteMessageBus
from agent.runtime.domain_events import (
    activate_runtime_domain_events,
    current_domain_event_context,
    emit_captured_domain_event,
    emit_domain_event,
)
from agent.runtime.events import RecordingEventSink, RuntimeContext


def test_task_events_reference_real_sqlite_task_rows(monkeypatch, tmp_path):
    """Create/claim/complete events are emitted only after store transactions."""
    task_system = importlib.import_module("agent.features.task_system")
    bus = SQLiteMessageBus(tmp_path / "team.sqlite3")
    monkeypatch.setattr(autonomous_tasks, "BUS", bus)
    autonomous_tasks.TASK_STORE._initialized = False
    monkeypatch.setattr(task_system, "TASK_STORE", autonomous_tasks.TASK_STORE)

    sink = RecordingEventSink("run_s6_task")
    with activate_runtime_domain_events(RuntimeContext(run_id="run_s6_task", events=sink)):
        created_json = task_system.create_task("persist real task")
        task_id = json.loads(created_json)["task_id"]
        assert task_system.claim_task(task_id, owner="lead").startswith("Claimed")
        assert task_system.complete_task(task_id).startswith("Completed")

    events = sink.snapshot()
    assert [event.type for event in events] == [
        "task.created", "task.claimed", "task.completed",
    ]
    assert all(event.payload["task_id"] == task_id for event in events)
    assert events[-1].payload["status"] == "completed"
    assert events[-1].payload["parent_run_id"] == "run_s6_task"


def test_background_worker_uses_explicit_captured_context():
    """ThreadPool workers publish to the parent run without sharing messages."""
    background = importlib.import_module("agent.features.background_tasks")
    with background._background_lock:
        background._background_tasks.clear()
        background._bg_counter = 0

    sink = RecordingEventSink("run_s6_background")
    block = SimpleNamespace(
        id="toolu_background",
        name="bash",
        input={"command": "echo done"},
    )
    with activate_runtime_domain_events(RuntimeContext(run_id="run_s6_background", events=sink)):
        captured = current_domain_event_context()
        background.start_background_task(
            block,
            lambda _block: "done",
            on_started=lambda task: emit_domain_event("background.started", {
                "background_id": task.id,
                "tool": task.tool_name,
            }),
            on_completed=lambda task: emit_captured_domain_event(
                captured,
                "background.completed",
                {
                    "background_id": task.id,
                    "tool": task.tool_name,
                    "status": task.status,
                },
            ),
        )

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if any(event.type == "background.completed" for event in sink.snapshot()):
            break
        time.sleep(0.01)

    events = sink.snapshot()
    assert [event.type for event in events] == [
        "background.started", "background.completed",
    ]
    assert events[-1].payload["status"] == "completed"
    assert all(event.payload["parent_run_id"] == "run_s6_background" for event in events)


def test_subagent_lifecycle_events_wrap_the_real_subagent_call(monkeypatch):
    """A subagent emits spawn/complete around its actual bounded model turn."""
    subagent = importlib.import_module("agent.features.subagent")
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="subagent result")],
    )
    monkeypatch.setattr(
        subagent,
        "get_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=lambda **_kwargs: response)),
    )

    sink = RecordingEventSink("run_s6_subagent")
    with activate_runtime_domain_events(RuntimeContext(run_id="run_s6_subagent", events=sink)):
        assert subagent.spawn_subagent("inspect one file") == "subagent result"

    events = sink.snapshot()
    assert [event.type for event in events] == ["agent.spawned", "agent.completed"]
    assert events[0].payload["agent_id"] == events[1].payload["agent_id"]
    assert events[1].payload["status"] == "completed"
