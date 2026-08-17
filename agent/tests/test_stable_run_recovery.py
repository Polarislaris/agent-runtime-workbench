"""S2/S3 integration tests for durable run replay and restart recovery."""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent.api.app import create_app
from agent.api.run_manager import RunManager
from agent.database.runs import RunStore
from agent.runtime.events import AgentLoopResult


def make_app(store_path, runner):
    """Build one lifecycle-scoped API process against a shared SQLite file."""
    manager = RunManager(
        agent_runner=runner,
        hook_collector=lambda *_args: [],
    )
    return create_app(
        run_manager_factory=lambda: manager,
        run_store_factory=lambda: RunStore(store_path),
        initialize_agent_runtime=False,
    ), manager


def test_completed_run_replays_after_a_new_api_process_starts(tmp_path):
    db_path = tmp_path / "runs.sqlite3"
    first_app, first_manager = make_app(
        db_path,
        lambda _messages, _runtime: AgentLoopResult.completed(),
    )

    with TestClient(first_app) as first_client:
        created = first_client.post("/api/runs", json={"prompt": "persist this run"})
        assert created.status_code == 201
        run_id = created.json()["id"]
        first_manager.get_run(run_id).worker.result(timeout=1)

    second_app, _second_manager = make_app(
        db_path,
        lambda _messages, _runtime: AgentLoopResult.completed(),
    )
    with TestClient(second_app) as second_client:
        listed = second_client.get("/api/runs")
        snapshot = second_client.get(f"/api/runs/{run_id}")
        replay = second_client.get(f"/api/runs/{run_id}/events")

    assert listed.status_code == 200
    assert listed.json()[0]["id"] == run_id
    assert snapshot.json()["messages"][0]["content"] == "persist this run"
    assert snapshot.json()["last_sequence"] == 2
    assert replay.status_code == 200
    assert "event: run.started" in replay.text
    assert "event: run.completed" in replay.text


def test_restart_marks_abandoned_run_interrupted_and_expires_permission(tmp_path):
    db_path = tmp_path / "runs.sqlite3"
    store = RunStore(db_path)
    store.create_run(
        "run_abandoned",
        "Awaiting approval",
        initial_messages=[{"role": "user", "content": "merge the worktree"}],
    )
    store.append_next_event(
        "run_abandoned",
        "run.started",
        {"status": "running"},
        event_id_factory=lambda: "evt_started",
        created_at="2026-08-17T08:00:00Z",
    )
    store.create_permission_request(
        "perm_abandoned",
        "run_abandoned",
        "merge_worktree",
        {"name": "review-a"},
        "Merge requires approval",
    )

    app, _manager = make_app(
        db_path,
        lambda _messages, _runtime: AgentLoopResult.completed(),
    )
    with TestClient(app) as client:
        snapshot = client.get("/api/runs/run_abandoned")

    body = snapshot.json()
    assert body["status"] == "interrupted"
    assert body["events"][-1]["type"] == "run.interrupted"
    assert body["last_sequence"] == 2
    assert RunStore(db_path).get_permission_request(
        "run_abandoned", "perm_abandoned"
    ).status == "expired"
