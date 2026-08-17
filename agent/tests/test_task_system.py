from __future__ import annotations

import importlib
import json

from agent.database.team_bus import SQLiteMessageBus


def _use_temp_task_db(monkeypatch, tmp_path):
    autonomous_tasks = importlib.import_module("agent.database.autonomous_tasks")
    bus = SQLiteMessageBus(tmp_path / "team.sqlite3")
    monkeypatch.setattr(autonomous_tasks, "BUS", bus)
    autonomous_tasks.TASK_STORE._initialized = False
    return autonomous_tasks.TASK_STORE

def test_task_dependencies_claim_and_complete(tmp_path, monkeypatch):
    _use_temp_task_db(monkeypatch, tmp_path)
    task_system = importlib.import_module("agent.features.task_system")

    schema = json.loads(task_system.create_task(
        subject="setup database schema",
        description="Create users table.",
    ))
    api = json.loads(task_system.create_task(
        subject="create API endpoints",
        description="Implement user CRUD endpoints.",
        blockedBy=[schema["id"]],
    ))

    blocked_claim = task_system.claim_task(api["id"], owner="agent")
    assert "blocked by" in blocked_claim

    assert "Claimed" in task_system.claim_task(schema["id"], owner="agent")
    completed = task_system.complete_task(schema["id"])

    assert "Completed" in completed
    assert api["id"] in completed
    assert "Claimed" in task_system.claim_task(api["id"], owner="agent")


def test_prompt_includes_task_section_when_task_tools_are_enabled(monkeypatch, tmp_path):
    _use_temp_task_db(monkeypatch, tmp_path)
    prompts = importlib.import_module("agent.prompts")
    monkeypatch.setattr(prompts, "MEMORY_INDEX", tmp_path / ".memory" / "MEMORY.md")
    prompts._last_context_key = None
    prompts._last_prompt = None

    enabled_tools = [
        "bash",
        "create_task",
        "list_tasks",
        "get_task",
        "claim_task",
        "complete_task",
        "list_task_events",
    ]
    context = prompts.update_context(messages=[], enabled_tools=enabled_tools)
    prompt = prompts.get_system_prompt(context)

    assert "Persistent task system:" in prompt
    assert "SQLite task board" in prompt
    assert "blockedBy dependencies" in prompt
