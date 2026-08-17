"""Hook registry and default hook implementations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from ..config import WORKDIR
from ..runtime.events import RuntimeContext
from .permissions import CliPermissionProvider, check_deny_list, check_rules


HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}

_DEFAULTS_REGISTERED = False


def register_hook(event: str, callback):
    """Register a callback for one hook event."""
    if event not in HOOKS:
        raise ValueError(f"Unknown hook event: {event}")
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """Run callbacks until one returns a non-None value."""
    if event not in HOOKS:
        raise ValueError(f"Unknown hook event: {event}")
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


def normalize_hook_message(result: Any) -> list[dict]:
    """Convert a UserPromptSubmit hook result into valid chat messages.

    Prompt-submit hooks may return a string, one message dictionary, or a list
    of either.  Keep this normalization at the hook boundary so arbitrary hook
    return values cannot leak into the conversation history.
    """
    if result is None:
        return []
    if isinstance(result, str):
        return [{"role": "user", "content": result}]
    if isinstance(result, Mapping):
        if "role" not in result or "content" not in result:
            raise ValueError("Hook message dictionaries require 'role' and 'content'")
        role = result["role"]
        if role not in {"user", "assistant"}:
            raise ValueError(f"Unsupported hook message role: {role!r}")
        return [{"role": role, "content": result["content"]}]
    if isinstance(result, list):
        messages = []
        for item in result:
            messages.extend(normalize_hook_message(item))
        return messages
    raise ValueError(
        "UserPromptSubmit hooks must return None, a string, a message dict, or a list"
    )


def collect_hook_messages(event: str, *args) -> list[dict]:
    """Run all hooks for an injection event and collect their chat messages."""
    if event not in HOOKS:
        raise ValueError(f"Unknown hook event: {event}")

    messages = []
    for callback in HOOKS[event]:
        messages.extend(normalize_hook_message(callback(*args)))
    return messages


def context_inject_hook(query: str) -> dict:
    """Inject concise, per-turn workspace context after a user prompt."""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return {
        "role": "user",
        "content": (
            "<preflight_context>\n"
            f"Active WORKDIR: {WORKDIR}\n"
            "</preflight_context>"
        ),
    }


def permission_hook(
    block,
    runtime: RuntimeContext | None = None,
) -> Optional[str]:
    """PreToolUse hook: apply hard deny and user-confirmation gates."""
    if block.name in {"bash", "test_worktree"}:
        reason = check_deny_list(block.input.get("command", ""))
        if reason:
            print(f"\n⛔ {reason}")
            return reason

    reason = check_rules(block.name, block.input)
    if reason:
        provider = (
            runtime.permissions
            if runtime is not None and runtime.permissions is not None
            else CliPermissionProvider()
        )
        decision = provider.decide(block.name, block.input, reason)
        if decision == "deny":
            return "Permission denied by user"

    return None


def log_hook(block, _runtime: RuntimeContext | None = None) -> Optional[str]:
    """PreToolUse hook: print a short trace before tool execution."""
    print(f"\033[90m[HOOK] PreToolUse: {block.name}(...)\033[0m")
    return None


def large_output_hook(block, output) -> Optional[str]:
    """PostToolUse hook: flag unusually large outputs."""
    if len(str(output)) > 100000:
        print(f"\033[90m[HOOK] PostToolUse: large output from {block.name}\033[0m")
    return None


def summary_hook(messages: list) -> Optional[str]:
    """Stop hook: print tool-result count for the session."""
    tool_count = sum(
        1
        for message in messages
        for block in (
            message.get("content")
            if isinstance(message.get("content"), list)
            else []
        )
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


def register_default_hooks() -> None:
    """Install the teaching defaults once."""
    global _DEFAULTS_REGISTERED
    if _DEFAULTS_REGISTERED:
        return

    register_hook("UserPromptSubmit", context_inject_hook)
    register_hook("PreToolUse", permission_hook)
    register_hook("PreToolUse", log_hook)
    register_hook("PostToolUse", large_output_hook)
    register_hook("Stop", summary_hook)
    _DEFAULTS_REGISTERED = True
