"""Permission gates for potentially risky tool calls."""

from __future__ import annotations

import re
from typing import Optional

from ..config import WORKDIR
from ..features.mcp import mcp_tool_annotations
from ..runtime.events import PermissionDecision


DENY_LIST = [
    "rm -rf /",
    "rm -fr /",
    "sudo",
    "shutdown",
    "reboot",
    "mkfs",
    "dd if=",
    "> /dev/sda",
]

OVERWRITE_REDIRECT_RE = re.compile(r"(^|[^>])>\s*[^&\s]")
TRUNCATE_RE = re.compile(r"\btruncate\b.*(\s-s\s*0\b|--size[=\s]+0\b)")


def is_inside_workspace(path: str) -> bool:
    """Return True when a tool path stays inside WORKDIR."""
    try:
        return (WORKDIR / path).resolve().is_relative_to(WORKDIR)
    except (OSError, ValueError):
        return False


def existing_file_text(path: str) -> Optional[str]:
    """Read an existing workspace file for permission checks."""
    try:
        file_path = (WORKDIR / path).resolve()
        if not file_path.is_relative_to(WORKDIR) or not file_path.is_file():
            return None
        return file_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def clears_existing_file(args: dict) -> bool:
    """Detect writes that empty an existing non-empty file."""
    old_text = existing_file_text(args.get("path", ""))
    new_content = args.get("content", "")
    return bool(old_text) and not new_content.strip()


def removes_file_content(args: dict) -> bool:
    """Detect edit_file calls that delete matched content."""
    old_text = args.get("old_text", "")
    new_text = args.get("new_text", "")
    return bool(old_text.strip()) and not new_text.strip()


def potentially_destructive_bash(args: dict) -> bool:
    """Detect shell commands that deserve explicit confirmation."""
    command = args.get("command", "")
    if any(kw in command for kw in ["rm ", "rmdir ", "> /etc/", "chmod 777", "chown "]):
        return True
    if OVERWRITE_REDIRECT_RE.search(command):
        return True
    return bool(TRUNCATE_RE.search(command))


def requires_merge_confirmation(args: dict) -> bool:
    """Require interactive approval for every merge_worktree call.

    The model-controlled user_confirmed flag is still checked inside the tool,
    but the hook makes the real terminal user approve the branch mutation too.
    """
    return True


def destructive_mcp_tool(tool_name: str, args: dict) -> bool:
    """Return whether MCP metadata marks this dynamic tool as destructive."""
    del args  # The MCP annotation, rather than arguments, defines its risk.
    return bool(mcp_tool_annotations(tool_name).get("destructiveHint"))


PERMISSION_RULES = [
    {
        "tools": ["write_file", "edit_file"],
        "check": lambda args, _tool_name=None: not is_inside_workspace(args.get("path", "")),
        "message": "Writing outside workspace",
    },
    {
        "tools": ["write_file"],
        "check": lambda args, _tool_name=None: clears_existing_file(args),
        "message": "Clearing an existing file",
    },
    {
        "tools": ["edit_file"],
        "check": lambda args, _tool_name=None: removes_file_content(args),
        "message": "Removing file content",
    },
    {
        "tools": ["bash", "test_worktree"],
        "check": lambda args, _tool_name=None: potentially_destructive_bash(args),
        "message": "Potentially destructive command",
    },
    {
        "tools": ["merge_worktree"],
        "check": lambda args, _tool_name=None: requires_merge_confirmation(args),
        "message": "Merging worktree requires explicit user confirmation",
    },
    {
        "tools": ["*"],
        "check": lambda args, tool_name: destructive_mcp_tool(tool_name, args),
        "message": "Destructive MCP tool requires explicit confirmation",
    },
]


def check_deny_list(command: str) -> Optional[str]:
    """Hard block commands that should never run."""
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None


def check_rules(tool_name: str, args: dict) -> Optional[str]:
    """Return the first matching permission reason, if any."""
    for rule in PERMISSION_RULES:
        if "*" not in rule["tools"] and tool_name not in rule["tools"]:
            continue
        try:
            if rule["check"](args, tool_name):
                return rule["message"]
        except (OSError, ValueError) as e:
            return f"Permission rule failed: {e}"
    return None


def ask_user(tool_name: str, args: dict, reason: str) -> str:
    """Ask the terminal user to allow or deny a gated action."""
    print(f"\n⚠  {reason}")
    print(f"   Tool: {tool_name}({args})")
    try:
        choice = input("   Allow? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "deny"
    return "allow" if choice in ("y", "yes") else "deny"


class CliPermissionProvider:
    """Interactive permission provider used by the existing terminal Agent."""

    def decide(
        self,
        tool_name: str,
        args: dict,
        reason: str,
    ) -> PermissionDecision:
        return ask_user(tool_name, args, reason)
