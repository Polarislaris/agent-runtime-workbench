from __future__ import annotations

from datetime import datetime
import importlib
import json


def reset_cron_module(cron_scheduler):
    with cron_scheduler._cron_lock:
        cron_scheduler._scheduled_jobs.clear()
        cron_scheduler._last_fired.clear()
    while True:
        try:
            cron_scheduler._cron_queue.get_nowait()
        except Exception:
            break


def test_cron_matches_basic_expressions():
    cron_scheduler = importlib.import_module("agent.features.cron_scheduler")
    sample = datetime(2026, 7, 30, 9, 0)

    assert cron_scheduler.cron_matches("0 9 * * *", sample)
    assert cron_scheduler.cron_matches("*/5 9 * * *", sample)
    assert cron_scheduler.cron_matches("0 9 * * 4", sample)
    assert not cron_scheduler.cron_matches("1 9 * * *", sample)


def test_schedule_list_cancel_cron(tmp_path, monkeypatch):
    cron_scheduler = importlib.import_module("agent.features.cron_scheduler")
    reset_cron_module(cron_scheduler)
    monkeypatch.setattr(
        cron_scheduler,
        "SCHEDULED_TASKS_FILE",
        tmp_path / ".scheduled_tasks.json",
    )

    created = json.loads(cron_scheduler.schedule_cron(
        cron="*/5 * * * *",
        prompt="run date",
        recurring=True,
        durable=True,
    ))

    listed = cron_scheduler.list_crons()
    assert created["id"] in listed
    assert cron_scheduler.SCHEDULED_TASKS_FILE.is_file()
    assert "Cancelled" in cron_scheduler.cancel_cron(created["id"])
    assert "(no cron jobs)" in cron_scheduler.list_crons()


def test_prompt_includes_cron_section(monkeypatch, tmp_path):
    prompts = importlib.import_module("agent.prompts")
    monkeypatch.setattr(prompts, "MEMORY_INDEX", tmp_path / ".memory" / "MEMORY.md")
    prompts._last_context_key = None
    prompts._last_prompt = None

    context = prompts.update_context(
        messages=[],
        enabled_tools=["schedule_cron", "list_crons", "cancel_cron"],
    )
    prompt = prompts.get_system_prompt(context)

    assert "Cron scheduler:" in prompt
    assert "produce scheduled prompts" in prompt
