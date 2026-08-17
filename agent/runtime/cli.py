"""Command-line interface for the standalone agent."""

from __future__ import annotations

import readline

from ..features.cron_scheduler import run_agent_turn_locked, start_cron_services
from ..tooling.hooks import collect_hook_messages, register_default_hooks
from .loop import agent_loop
from ..features.skills import scan_skills


def configure_readline() -> None:
    """Configure readline for better Chinese input behavior on macOS libedit."""
    try:
        readline.parse_and_bind("set bind-tty-special-chars off")
        readline.parse_and_bind("set input-meta on")
        readline.parse_and_bind("set output-meta on")
        readline.parse_and_bind("set convert-meta off")
    except Exception:
        # Readline is a convenience; the agent should still run without it.
        pass


def print_final_text(history: list) -> None:
    """Print the final assistant text blocks from the latest turn."""
    response_content = history[-1]["content"]
    if isinstance(response_content, list):
        for block in response_content:
            if getattr(block, "type", None) == "text":
                print(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                print(block.get("text", ""))


def main() -> None:
    """Interactive terminal entry point."""
    configure_readline()
    scan_skills()
    register_default_hooks()

    print("Agent: Memory-enabled Coding Agent")
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    start_cron_services(history, agent_loop, print_final_text)
    while True:
        try:
            query = input("\033[36magent >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        injected_messages = collect_hook_messages("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        history.extend(injected_messages)
        run_agent_turn_locked(history, agent_loop, print_final_text)
        print()
