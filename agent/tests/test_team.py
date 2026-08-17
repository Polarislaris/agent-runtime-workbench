from __future__ import annotations

import importlib
import sys
import types

from agent.database.team_bus import SQLiteMessageBus


def test_inject_inbox_messages_acks_after_history_append(monkeypatch, tmp_path):
    team = importlib.import_module("agent.features.team")
    bus = SQLiteMessageBus(tmp_path / "team.sqlite3")
    monkeypatch.setattr(team, "BUS", bus)

    message = bus.send("alice", "lead", "Schema done.", msg_type="result")
    history = []

    injected = team.inject_inbox_messages("lead", history)

    assert injected == 1
    assert history
    assert "<inbox>" in history[-1]["content"]
    assert f"message_id={message.id}" in history[-1]["content"]
    assert bus.claim_inbox("lead") == []


def test_send_and_check_inbox_use_sqlite_bus(monkeypatch, tmp_path):
    team = importlib.import_module("agent.features.team")
    bus = SQLiteMessageBus(tmp_path / "team.sqlite3")
    monkeypatch.setattr(team, "BUS", bus)

    sent = team.run_send_message("alice", "Please inspect models.", from_agent="lead")
    assert "Sent message" in sent

    inbox = team.run_check_inbox("alice")
    assert "Please inspect models." in inbox
    assert "from=lead" in inbox

    # check_inbox stages acks until loop.py has appended the tool_result into
    # history; the test simulates that final loop acknowledgement.
    assert bus.claim_inbox("alice") == []
    assert team.acknowledge_staged_inbox_messages() == 1
    assert bus.claim_inbox("alice") == []


def test_prompt_includes_team_section(monkeypatch, tmp_path):
    prompts = importlib.import_module("agent.prompts")
    prompts._last_context_key = None
    prompts._last_prompt = None
    monkeypatch.setattr(prompts, "MEMORY_INDEX", tmp_path / ".memory" / "MEMORY.md")
    monkeypatch.setattr(prompts, "TEAM_DB", tmp_path / "team.sqlite3")

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

    assert "Team system:" in prompt
    assert "SQLite message bus:" in prompt
    assert "Team protocols:" in prompt


def test_subagent_does_not_expose_team_tools():
    sys.modules.setdefault("anthropic", types.SimpleNamespace(Anthropic=object))
    subagent = importlib.import_module("agent.features.subagent")
    tool_names = {tool["name"] for tool in subagent.SUB_TOOLS}

    assert "spawn_teammate" not in tool_names
    assert "send_message" not in tool_names
    assert "check_inbox" not in tool_names
    assert "submit_plan" not in tool_names
