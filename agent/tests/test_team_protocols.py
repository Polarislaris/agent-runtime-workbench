from __future__ import annotations

import importlib

from agent.database.team_bus import SQLiteMessageBus


def isolated_protocols(monkeypatch, tmp_path):
    protocols = importlib.import_module("agent.database.team_protocols")
    bus = SQLiteMessageBus(tmp_path / "team.sqlite3")
    monkeypatch.setattr(protocols, "BUS", bus)
    protocols.PROTOCOLS._initialized = False
    return protocols, bus


def test_shutdown_request_response_updates_sqlite_state(monkeypatch, tmp_path):
    protocols, bus = isolated_protocols(monkeypatch, tmp_path)

    request = protocols.PROTOCOLS.create_request(
        "shutdown",
        "lead",
        "alice",
        "finish current work",
    )

    alice_history = []
    alice_result = protocols.consume_teammate_inbox("alice", alice_history)

    assert alice_result.shutdown_requested
    assert bus.claim_inbox("alice") == []

    lead_history = []
    assert protocols.consume_lead_inbox(lead_history) == 1

    resolved = protocols.PROTOCOLS.get_request(request.request_id)
    assert resolved is not None
    assert resolved.status == "approved"
    assert "shutdown_response" in lead_history[-1]["content"]


def test_submit_plan_review_plan_and_teammate_response(monkeypatch, tmp_path):
    protocols, bus = isolated_protocols(monkeypatch, tmp_path)

    submitted = protocols.submit_plan("Refactor auth after adding tests.", from_agent="alice")
    request_id = submitted.rsplit(" ", 1)[-1]

    pending = protocols.PROTOCOLS.get_request(request_id)
    assert pending is not None
    assert pending.status == "pending"

    reviewed = protocols.review_plan(request_id, approve=True, reason="Looks safe.")
    assert "approved" in reviewed

    alice_history = []
    result = protocols.consume_teammate_inbox("alice", alice_history)

    assert result.has_work
    assert "Plan approved" in alice_history[-1]["content"]
    assert bus.claim_inbox("alice") == []


def test_duplicate_or_wrong_response_does_not_rewrite_final_state(monkeypatch, tmp_path):
    protocols, _bus = isolated_protocols(monkeypatch, tmp_path)

    state = protocols.PROTOCOLS.create_request(
        "plan_approval",
        "alice",
        "lead",
        "Risky auth refactor plan.",
    )
    protocols.PROTOCOLS.review_plan(state.request_id, approve=True, reason="ok")

    duplicate = protocols.PROTOCOLS.match_response(
        "plan_approval_response",
        state.request_id,
        from_agent="lead",
        to_agent="alice",
        approve=False,
        response_payload="late rejection",
    )

    assert duplicate is not None
    final = protocols.PROTOCOLS.get_request(state.request_id)
    assert final is not None
    assert final.status == "approved"
    assert final.response_payload == "ok"


def test_protocol_prompt_section(monkeypatch, tmp_path):
    prompts = importlib.import_module("agent.prompts")
    protocols, bus = isolated_protocols(monkeypatch, tmp_path)
    monkeypatch.setattr(prompts, "MEMORY_INDEX", tmp_path / ".memory" / "MEMORY.md")
    monkeypatch.setattr(prompts, "TEAM_DB", bus.db_path)
    monkeypatch.setattr(prompts, "_pending_protocol_count", lambda: 0)
    prompts._last_context_key = None
    prompts._last_prompt = None

    context = prompts.update_context(
        messages=[],
        enabled_tools=[
            "spawn_teammate",
            "send_message",
            "check_inbox",
            "request_shutdown",
            "request_plan",
            "review_plan",
            "submit_plan",
        ],
    )
    prompt = prompts.get_system_prompt(context)

    assert "Team protocols:" in prompt
    assert "pending -> approved/rejected/expired/failed" in prompt
