"""Focused subagent runner for the task tool."""

from __future__ import annotations

from uuid import uuid4

from ..runtime.client import get_client
from ..config import MODEL
from ..tooling.fs import run_bash, run_edit, run_glob, run_read, run_write
from ..tooling.hooks import trigger_hooks
from ..runtime.messages import extract_text
from ..prompts import SUB_SYSTEM
from ..tooling.schemas import TOOLS
from ..runtime.domain_events import emit_domain_event


SUB_TOOL_NAMES = {"bash", "read_file", "write_file", "edit_file", "glob"}
SUB_TOOLS = [tool for tool in TOOLS if tool["name"] in SUB_TOOL_NAMES]

SUB_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


def spawn_subagent(description: str) -> str:
    """Run a clean-context subagent and return only its final summary."""
    description = str(description).strip()
    if not description:
        return "Error: task description must not be empty"

    agent_id = f"subagent_{uuid4().hex[:12]}"
    emit_domain_event("agent.spawned", {
        "agent_id": agent_id,
        "agent_kind": "subagent",
        "status": "running",
    })
    print(f"\033[95m[Subagent spawned] {description}\033[0m")
    messages = [{"role": "user", "content": description}]
    last_text = "(subagent did not finish)"

    try:
        for _ in range(30):
            response = get_client().messages.create(
                model=MODEL,
                system=SUB_SYSTEM,
                messages=messages,
                tools=SUB_TOOLS,
                max_tokens=8000,
            )

            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                last_text = extract_text(response.content)
                break

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    print(f"\033[31m[sub] Permission denied: {block.name} {block.input}\033[0m")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(blocked),
                        "is_error": True,
                    })
                    continue

                print(f"\033[35m[sub] $ {block.name} {block.input}\033[0m")
                handler = SUB_HANDLERS.get(block.name)
                if not handler:
                    output = f"Error: Unknown subagent tool: {block.name}"
                else:
                    try:
                        output = handler(**block.input)
                    except TypeError as error:
                        output = f"Error: Invalid tool input for {block.name}: {error}"

                trigger_hooks("PostToolUse", block, output)
                print(f"[sub] {output[:200]}")

                result = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
                if output.startswith("Error:"):
                    result["is_error"] = True
                results.append(result)

            messages.append({"role": "user", "content": results})
        else:
            last_text = extract_text(messages[-1].get("content", ""))
            last_text = f"Subagent stopped after 30 turns. Last result:\n{last_text}"
    except Exception as error:
        emit_domain_event("agent.completed", {
            "agent_id": agent_id,
            "agent_kind": "subagent",
            "status": "failed",
            "error": str(error),
        })
        return f"Error: Subagent failed: {error}"

    emit_domain_event("agent.completed", {
        "agent_id": agent_id,
        "agent_kind": "subagent",
        "status": "completed",
    })
    print("\033[95m[Subagent done]\033[0m")
    return last_text
