"""Regression coverage for the s20 harness alignment changes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.features.mcp import connect_mcp, reset_mock_mcp_clients
from agent.runtime import loop
from agent.tooling import hooks
from agent.tooling.permissions import check_rules
from agent.tooling.pool import assemble_tool_pool


@pytest.fixture(autouse=True)
def isolate_hooks_and_mcp():
    saved_hooks = {event: list(callbacks) for event, callbacks in hooks.HOOKS.items()}
    for callbacks in hooks.HOOKS.values():
        callbacks.clear()
    reset_mock_mcp_clients()
    yield
    for event, callbacks in hooks.HOOKS.items():
        callbacks[:] = saved_hooks[event]
    reset_mock_mcp_clients()


def test_user_prompt_submit_collects_all_normalized_injections():
    hooks.register_hook("UserPromptSubmit", lambda _query: "first")
    hooks.register_hook("UserPromptSubmit", lambda _query: {
        "role": "user", "content": "second",
    })
    hooks.register_hook("UserPromptSubmit", lambda _query: [
        {"role": "user", "content": "third"},
        "fourth",
    ])

    assert hooks.collect_hook_messages("UserPromptSubmit", "refactor auth") == [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
        {"role": "user", "content": "third"},
        {"role": "user", "content": "fourth"},
    ]


def test_actual_tool_blocks_override_non_tool_stop_reason(monkeypatch):
    tool_block = SimpleNamespace(id="toolu_1", name="echo", input={"value": "ok"}, type="tool_use")
    responses = iter([
        SimpleNamespace(stop_reason="end_turn", content=[tool_block]),
        SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="done")],
        ),
    ])
    calls = []

    class FakeMessages:
        def create(self, **_kwargs):
            return next(responses)

    monkeypatch.setattr(loop, "get_client", lambda: SimpleNamespace(messages=FakeMessages()))
    monkeypatch.setattr(loop, "assemble_tool_pool", lambda: (
        [{"name": "echo"}],
        {"echo": lambda value: calls.append(value) or "echoed"},
    ))
    monkeypatch.setattr(loop, "collect_runtime_notifications", lambda: [])
    monkeypatch.setattr(loop, "consume_lead_inbox", lambda _messages, **_kwargs: None)
    monkeypatch.setattr(loop, "acknowledge_staged_inbox_messages", lambda: None)
    monkeypatch.setattr(loop, "load_memories", lambda messages: messages)
    monkeypatch.setattr(loop, "update_context", lambda **_kwargs: {})
    monkeypatch.setattr(loop, "get_system_prompt", lambda _context: "system")
    monkeypatch.setattr(loop, "estimate_token_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(loop, "extract_memories", lambda _messages: None)
    monkeypatch.setattr(loop, "consolidate_memories", lambda: None)

    history = [{"role": "user", "content": "run it"}]
    loop.agent_loop(history)

    assert calls == ["ok"]
    assert any(
        block.get("type") == "tool_result" and block.get("content") == "echoed"
        for message in history
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict)
    )


def test_destructive_mcp_metadata_requires_permission_but_read_only_does_not():
    connect_mcp("deploy")
    tools, _handlers = assemble_tool_pool()
    by_name = {tool["name"]: tool for tool in tools}

    assert by_name["mcp__deploy__trigger"]["annotations"]["destructiveHint"] is True
    assert by_name["mcp__deploy__get_status"]["annotations"]["readOnlyHint"] is True
    assert check_rules("mcp__deploy__trigger", {"environment": "prod"}) == (
        "Destructive MCP tool requires explicit confirmation"
    )
    assert check_rules("mcp__deploy__get_status", {}) is None
    assert check_rules("read_file", {"path": "README.md"}) is None
