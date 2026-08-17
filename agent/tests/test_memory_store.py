from __future__ import annotations

from pathlib import Path
import importlib
import sys
import types


def test_write_memory_file_rebuilds_index(tmp_path, monkeypatch):
    # agent.features.memory imports agent.runtime.client, which imports anthropic. This smoke test
    # does not call the API, so a tiny fake module is enough.
    fake_anthropic = types.ModuleType("anthropic")

    class FakeAnthropic:
        def __init__(self, *args, **kwargs):
            self.messages = types.SimpleNamespace(create=None)

    fake_anthropic.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    memory = importlib.import_module("agent.features.memory")
    memory_dir = Path(tmp_path) / ".memory"
    monkeypatch.setattr(memory, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(memory, "MEMORY_INDEX", memory_dir / "MEMORY.md")

    path = memory.write_memory_file(
        name="user preference tabs",
        mem_type="user",
        description="User prefers tabs for indentation",
        body="Use tabs instead of spaces when editing code.",
    )

    assert path.name == "user-preference-tabs.md"
    assert path.is_file()
    assert memory.MEMORY_INDEX.is_file()
    assert "User prefers tabs for indentation" in memory.read_memory_index()
