"""Step 6 contract tests for REST endpoints and terminal SSE replay."""

from __future__ import annotations

from threading import Event
import time

from fastapi.testclient import TestClient

from agent.api.app import create_app
from agent.api.run_manager import RunManager
from agent.database.runs import RunStore
from agent.runtime.events import AgentLoopResult


def make_client(runner, tmp_path, **manager_kwargs):
    manager = RunManager(
        agent_runner=runner,
        hook_collector=lambda *_args: [],
        **manager_kwargs,
    )
    app = create_app(
        run_manager_factory=lambda: manager,
        run_store_factory=lambda: RunStore(tmp_path / "runs.sqlite3"),
        initialize_agent_runtime=False,
    )
    return TestClient(app), manager


def wait_for_api_event(
    client: TestClient,
    run_id: str,
    event_type: str,
    timeout: float = 1.0,
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200
        for event in response.json()["events"]:
            if event["type"] == event_type:
                return event
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {event_type}")


def test_create_list_and_get_run_contract(tmp_path):
    client, manager = make_client(
        lambda _messages, _runtime: AgentLoopResult.completed(),
        tmp_path,
    )
    with client:
        created = client.post("/api/runs", json={"prompt": "  inspect API  "})
        assert created.status_code == 201
        run_id = created.json()["id"]
        manager.get_run(run_id).worker.result(timeout=1)

        fetched = client.get(f"/api/runs/{run_id}")
        listed = client.get("/api/runs")

    assert fetched.status_code == 200
    assert fetched.json()["status"] == "completed"
    assert fetched.json()["messages"][0]["content"] == "inspect API"
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == run_id


def test_run_history_supports_status_filter_and_opaque_cursor(tmp_path):
    client, manager = make_client(
        lambda _messages, _runtime: AgentLoopResult.completed(),
        tmp_path,
    )
    with client:
        first = client.post("/api/runs", json={"prompt": "first history"}).json()
        manager.get_run(first["id"]).worker.result(timeout=1)
        second = client.post("/api/runs", json={"prompt": "second history"}).json()
        manager.get_run(second["id"]).worker.result(timeout=1)

        page_one = client.get("/api/runs?status=completed&limit=1")
        cursor = page_one.headers.get("X-Next-Cursor")
        page_two = client.get(f"/api/runs?status=completed&limit=1&cursor={cursor}")
        invalid = client.get("/api/runs?cursor=run_missing")

    assert page_one.status_code == 200
    assert cursor == page_one.json()[0]["id"]
    assert page_two.status_code == 200
    assert page_two.json()[0]["id"] != page_one.json()[0]["id"]
    assert invalid.status_code == 422


def test_create_validation_conflict_and_not_found_mapping(tmp_path):
    entered = Event()
    release = Event()

    def blocking_runner(_messages, _runtime):
        entered.set()
        release.wait(1)
        return AgentLoopResult.completed()

    client, manager = make_client(blocking_runner, tmp_path)
    with client:
        assert client.post("/api/runs", json={"prompt": "  "}).status_code == 422
        first = client.post("/api/runs", json={"prompt": "first"})
        assert first.status_code == 201
        assert entered.wait(1)

        conflict = client.post("/api/runs", json={"prompt": "second"})
        missing = client.get("/api/runs/run_missing")
        release.set()
        manager.get_run(first.json()["id"]).worker.result(timeout=1)

    assert conflict.status_code == 409
    assert missing.status_code == 404


def test_cancel_endpoint_sets_cooperative_token(tmp_path):
    entered = Event()

    def cancellable_runner(_messages, runtime):
        entered.set()
        runtime.cancellation.wait(1)
        return AgentLoopResult.cancelled()

    client, manager = make_client(cancellable_runner, tmp_path)
    with client:
        created = client.post("/api/runs", json={"prompt": "cancel"}).json()
        assert entered.wait(1)
        cancelled = client.post(f"/api/runs/{created['id']}/cancel")
        manager.get_run(created["id"]).worker.result(timeout=1)

    assert cancelled.status_code == 200
    assert manager.get_run(created["id"]).cancellation.is_cancelled()
    assert manager.snapshot(created["id"]).status == "cancelled"


def test_permission_endpoint_resolves_waiting_web_provider(tmp_path):
    decisions: list[str] = []

    def permission_runner(_messages, runtime):
        decisions.append(runtime.permissions.decide("bash", {}, "confirm"))
        return AgentLoopResult.completed()

    client, manager = make_client(
        permission_runner,
        tmp_path,
        permission_timeout_seconds=2,
    )
    with client:
        created = client.post("/api/runs", json={"prompt": "approve"}).json()
        requested = wait_for_api_event(
            client,
            created["id"],
            "permission.requested",
        )
        request_id = requested["payload"]["request_id"]

        resolved = client.post(
            f"/api/runs/{created['id']}/permissions/{request_id}",
            json={"decision": "allow"},
        )
        duplicate = client.post(
            f"/api/runs/{created['id']}/permissions/{request_id}",
            json={"decision": "deny"},
        )
        invalid = client.post(
            f"/api/runs/{created['id']}/permissions/{request_id}",
            json={"decision": "maybe"},
        )
        manager.get_run(created["id"]).worker.result(timeout=1)

    assert resolved.status_code == 204
    assert duplicate.status_code == 409
    assert invalid.status_code == 422
    assert decisions == ["allow"]


def test_sse_replays_events_after_sequence_and_closes_at_terminal(tmp_path):
    client, manager = make_client(
        lambda _messages, _runtime: AgentLoopResult.completed(),
        tmp_path,
    )
    with client:
        created = client.post("/api/runs", json={"prompt": "stream"}).json()
        manager.get_run(created["id"]).worker.result(timeout=1)

        response = client.get(f"/api/runs/{created['id']}/events?after=1")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: run.completed" in response.text
    assert "event: run.started" not in response.text
    assert '"sequence":2' in response.text
    assert response.text.endswith("\n\n")


def test_sse_last_event_id_uses_the_durable_sequence_cursor(tmp_path):
    client, manager = make_client(
        lambda _messages, _runtime: AgentLoopResult.completed(),
        tmp_path,
    )
    with client:
        created = client.post("/api/runs", json={"prompt": "header cursor"}).json()
        manager.get_run(created["id"]).worker.result(timeout=1)
        started_event_id = manager.snapshot(created["id"]).events[0]["id"]
        response = client.get(
            f"/api/runs/{created['id']}/events?after=0",
            headers={"Last-Event-ID": started_event_id},
        )

    assert response.status_code == 200
    assert "event: run.started" not in response.text
    assert "event: run.completed" in response.text


def test_snapshot_and_sse_apply_the_same_secret_redaction(tmp_path):
    def sensitive_runner(_messages, runtime):
        runtime.message_journal.append({
            "role": "assistant",
            "content": "Authorization: Bearer exposed-token",
        })
        runtime.events.emit("tool.completed", {
            "tool": "bash",
            "input_summary": {"api_key": "should-not-leak"},
            "output_preview": "SERVICE_TOKEN=also-not-visible",
        })
        return AgentLoopResult.completed()

    client, manager = make_client(sensitive_runner, tmp_path)
    with client:
        created = client.post("/api/runs", json={"prompt": "redact values"}).json()
        manager.get_run(created["id"]).worker.result(timeout=1)
        snapshot = client.get(f"/api/runs/{created['id']}")
        stream = client.get(f"/api/runs/{created['id']}/events")

    body = snapshot.json()
    serialized = snapshot.text + stream.text
    assert body["messages"][-1]["content"] == "Authorization: Bearer ***"
    assert body["events"][-2]["payload"]["input_summary"]["api_key"] == "***"
    assert "should-not-leak" not in serialized
    assert "also-not-visible" not in serialized


def test_sse_unknown_run_returns_404(tmp_path):
    client, _manager = make_client(
        lambda _messages, _runtime: AgentLoopResult.completed(),
        tmp_path,
    )
    with client:
        response = client.get("/api/runs/run_missing/events")

    assert response.status_code == 404
