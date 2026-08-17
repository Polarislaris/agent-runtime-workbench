from __future__ import annotations

import importlib


def test_prompt_omits_empty_memory_section(monkeypatch, tmp_path):
    prompts = importlib.import_module("agent.prompts")
    monkeypatch.setattr(prompts, "MEMORY_INDEX", tmp_path / ".memory" / "MEMORY.md")
    prompts._last_context_key = None
    prompts._last_prompt = None

    context = prompts.update_context(
        messages=[],
        enabled_tools=["bash", "read_file"],
    )
    prompt = prompts.get_system_prompt(context)

    assert "Enabled tools:" in prompt
    assert "- bash" in prompt
    assert "Persistent memory index:" not in prompt


def test_prompt_loads_memory_section_from_real_index(monkeypatch, tmp_path):
    prompts = importlib.import_module("agent.prompts")
    memory_index = tmp_path / ".memory" / "MEMORY.md"
    memory_index.parent.mkdir()
    memory_index.write_text(
        "- [user-preference-tabs](user-preference-tabs.md) — User prefers tabs (user)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(prompts, "MEMORY_INDEX", memory_index)
    prompts._last_context_key = None
    prompts._last_prompt = None

    context = prompts.update_context(
        messages=[],
        enabled_tools=["bash"],
    )
    prompt = prompts.get_system_prompt(context)

    assert "Persistent memory index:" in prompt
    assert "User prefers tabs" in prompt


def test_prompt_cache_reuses_prompt_for_same_context(monkeypatch, tmp_path):
    prompts = importlib.import_module("agent.prompts")
    monkeypatch.setattr(prompts, "MEMORY_INDEX", tmp_path / ".memory" / "MEMORY.md")
    prompts._last_context_key = None
    prompts._last_prompt = None

    context = prompts.update_context(
        messages=[],
        enabled_tools=["bash"],
    )
    first = prompts.get_system_prompt(context)
    second = prompts.get_system_prompt(context)

    assert first is second
