"""Compatibility tests for CLI and runtime-aware permission hooks."""

from __future__ import annotations

from types import SimpleNamespace

from agent.runtime.events import RuntimeContext
from agent.tooling import permissions
from agent.tooling.hooks import log_hook, permission_hook


class FixedProvider:
    def __init__(self, decision: str) -> None:
        self.decision = decision
        self.calls = []

    def decide(self, tool_name: str, args: dict, reason: str) -> str:
        self.calls.append((tool_name, args, reason))
        return self.decision


def test_cli_permission_provider_preserves_terminal_prompt(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    decision = permissions.CliPermissionProvider().decide(
        "merge_worktree",
        {"name": "review-a"},
        "confirm",
    )

    assert decision == "allow"


def test_cli_permission_provider_fails_closed_on_eof(monkeypatch):
    def raise_eof(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    assert permissions.CliPermissionProvider().decide("bash", {}, "confirm") == "deny"


def test_permission_hook_uses_runtime_provider_for_gated_tool():
    provider = FixedProvider("deny")
    runtime = RuntimeContext(permissions=provider)
    block = SimpleNamespace(
        name="merge_worktree",
        input={"name": "review-a", "user_confirmed": True},
    )

    result = permission_hook(block, runtime)

    assert result == "Permission denied by user"
    assert provider.calls[0][0] == "merge_worktree"


def test_hard_deny_does_not_ask_any_provider():
    provider = FixedProvider("allow")
    runtime = RuntimeContext(permissions=provider)
    block = SimpleNamespace(name="bash", input={"command": "sudo reboot"})

    result = permission_hook(block, runtime)

    assert "deny list" in result
    assert provider.calls == []


def test_pre_tool_log_hook_accepts_old_and_runtime_aware_calls():
    block = SimpleNamespace(name="read_file", input={"path": "README.md"})

    assert log_hook(block) is None
    assert log_hook(block, RuntimeContext()) is None
