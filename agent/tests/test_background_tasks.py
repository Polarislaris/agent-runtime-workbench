from __future__ import annotations

import importlib
import time
import types


def reset_background_module(background):
    with background._background_lock:
        background._background_tasks.clear()
        background._bg_counter = 0
    while True:
        try:
            background._completion_queue.get_nowait()
        except Exception:
            break


def test_should_run_background_uses_explicit_flag_and_heuristic():
    background = importlib.import_module("agent.features.background_tasks")

    assert background.should_run_background(
        "bash",
        {"command": "echo fast", "run_in_background": True},
    )
    assert background.should_run_background(
        "bash",
        {"command": "npm install"},
    )
    assert not background.should_run_background(
        "read_file",
        {"path": "README.md"},
    )


def test_start_background_task_collects_notification():
    background = importlib.import_module("agent.features.background_tasks")
    reset_background_module(background)

    block = types.SimpleNamespace(
        id="toolu_1",
        name="bash",
        input={"command": "echo done", "run_in_background": True},
    )
    bg_id = background.start_background_task(block, lambda _block: "done")

    notifications = []
    for _ in range(20):
        notifications = background.collect_background_results()
        if notifications:
            break
        time.sleep(0.01)

    assert bg_id == "bg_0001"
    assert notifications
    assert notifications[0].task_id == "bg_0001"
    assert "<task_id>bg_0001</task_id>" in notifications[0].text
    assert "<status>completed</status>" in notifications[0].text
    assert "done" in notifications[0].text

    # Collecting only drains the notification queue. The task state remains
    # available until loop.py has injected the notification into messages.
    assert "bg_0001" in background._background_tasks
    background.acknowledge_background_notifications(notifications)
    assert "bg_0001" not in background._background_tasks


def test_prompt_includes_background_section_for_bash(monkeypatch, tmp_path):
    prompts = importlib.import_module("agent.prompts")
    monkeypatch.setattr(prompts, "MEMORY_INDEX", tmp_path / ".memory" / "MEMORY.md")
    prompts._last_context_key = None
    prompts._last_prompt = None

    context = prompts.update_context(messages=[], enabled_tools=["bash"])
    prompt = prompts.get_system_prompt(context)

    assert "Background task policy:" in prompt
    assert "Task-system blockedBy dependencies take priority" in prompt
