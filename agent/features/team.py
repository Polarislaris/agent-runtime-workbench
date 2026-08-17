"""Team orchestration for s15 persistent teammates.

Team agents differ from the older `task` subagent: a teammate has a name, a
role, its own conversation history, and a durable SQLite inbox for follow-up
communication across turns.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time
from typing import Optional

from ..config import (
    MAX_TEAMMATES,
    MODEL,
    TEAM_AGENT_ID,
    TEAM_CLAIM_TIMEOUT_SECONDS,
    TEAM_INBOX_LIMIT,
    TEAMMATE_MAX_TURNS,
    WORKDIR,
)
from ..database.autonomous_tasks import TASK_STORE
from ..database.worktrees import WORKTREE_STORE
from ..features.autonomous import idle_poll_for_work
from ..features.task_system import claim_task, complete_task, list_tasks
from ..tooling.fs import run_bash, run_edit, run_glob, run_read, run_write
from ..tooling.hooks import trigger_hooks
from ..runtime.messages import extract_text
from ..runtime.domain_events import (
    DomainEventContext,
    activate_domain_events,
    current_domain_event_context,
    emit_domain_event,
)
from ..database.team_bus import BUS, Message
from ..database.team_protocols import (
    consume_lead_inbox,
    consume_teammate_inbox,
    submit_plan,
)
from ..tooling.schemas import TOOLS


TEAM_TOOL_NAMES = {
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "send_message",
    "check_inbox",
    "submit_plan",
    "list_tasks",
    "claim_task",
    "complete_task",
}
TEAMMATE_TOOLS = [tool for tool in TOOLS if tool["name"] in TEAM_TOOL_NAMES]


@dataclass
class TeammateState:
    """In-memory lifecycle record for one long-lived teammate thread."""

    name: str
    role: str
    status: str
    created_at: float
    last_seen_at: float
    thread: Optional[threading.Thread] = None
    error: Optional[str] = None
    current_cwd: str = str(WORKDIR)
    current_worktree: Optional[str] = None


active_teammates: dict[str, TeammateState] = {}
_team_lock = threading.Lock()
_pending_ack_by_thread: dict[int, list[int]] = {}
_pending_ack_lock = threading.Lock()


def _format_inbox(messages: list[Message]) -> str:
    """Format durable messages for prompt injection with stable message ids."""
    lines = ["<inbox>"]
    for message in messages:
        lines.append(
            f"[message_id={message.id} from={message.from_agent} type={message.type}]"
        )
        lines.append(message.content)
    lines.append("</inbox>")
    return "\n".join(lines)


def inject_inbox_messages(agent_id: str, history: list) -> int:
    """Claim inbox messages, append them to history, then ack after injection.

    This preserves the important consistency rule: a message is marked consumed
    only after it has actually entered the receiving agent's conversation.
    """
    BUS.release_stale_claims(TEAM_CLAIM_TIMEOUT_SECONDS)
    claimed = BUS.claim_inbox(agent_id, limit=TEAM_INBOX_LIMIT)
    if not claimed:
        return 0

    message_ids = [message.id for message in claimed]
    try:
        history.append({
            "role": "user",
            "content": _format_inbox(claimed),
        })
    except Exception as exc:
        # If injection fails, release the claim so a later turn can retry.
        BUS.release_messages(message_ids)
        raise exc

    BUS.ack_messages(message_ids)
    return len(claimed)


def _stage_inbox_ack(message_ids: list[int]) -> None:
    """Remember claimed inbox ids until the current thread appends tool_result."""
    if not message_ids:
        return
    thread_id = threading.get_ident()
    with _pending_ack_lock:
        _pending_ack_by_thread.setdefault(thread_id, []).extend(message_ids)


def acknowledge_staged_inbox_messages() -> int:
    """Ack check_inbox messages after the caller has appended tool results.

    The staging key is the current thread id. This prevents the Lead loop from
    accidentally acknowledging a teammate thread's claimed inbox messages before
    that teammate has injected them into its own conversation.
    """
    thread_id = threading.get_ident()
    with _pending_ack_lock:
        message_ids = _pending_ack_by_thread.pop(thread_id, [])
    if not message_ids:
        return 0
    return BUS.ack_messages(message_ids)


def run_send_message(
    to_agent: str,
    content: str,
    msg_type: str = "message",
    from_agent: str = TEAM_AGENT_ID,
) -> str:
    """Tool handler: send one durable team message through SQLite."""
    try:
        message = _send_team_message(
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            msg_type=msg_type,
        )
        return (
            f"Sent message {message.id} from {message.from_agent} "
            f"to {message.to_agent} ({message.type})."
        )
    except ValueError as exc:
        return f"Error: {exc}"


def _send_team_message(
    from_agent: str,
    to_agent: str,
    content: str,
    msg_type: str,
):
    """Persist a team message first, then report its durable identifier."""
    message = BUS.send(
        from_agent=from_agent,
        to_agent=to_agent,
        content=content,
        msg_type=msg_type,
    )
    emit_domain_event("team.message", {
        "message_id": message.id,
        "from_agent": message.from_agent,
        "to_agent": message.to_agent,
        "message_type": message.type,
    })
    return message


def run_check_inbox(agent: str = TEAM_AGENT_ID) -> str:
    """Tool handler: consume and display inbox messages for one agent."""
    if agent == TEAM_AGENT_ID:
        temp_history: list = []
        count = consume_lead_inbox(temp_history)
        if not count:
            return f"Inbox for {agent} is empty."
        return temp_history[-1]["content"]

    BUS.release_stale_claims(TEAM_CLAIM_TIMEOUT_SECONDS)
    try:
        claimed = BUS.claim_inbox(agent, limit=TEAM_INBOX_LIMIT)
    except ValueError as exc:
        return f"Error: {exc}"

    if not claimed:
        return f"Inbox for {agent} is empty."

    message_ids = [message.id for message in claimed]
    text = _format_inbox(claimed)
    # The tool handler cannot append its own tool_result into history. Stage the
    # ids so loop.py or the teammate loop can ack only after that append happens.
    _stage_inbox_ack(message_ids)
    return text


def list_teammates() -> str:
    """Tool handler: show currently known teammate threads and their status."""
    with _team_lock:
        if not active_teammates:
            return "(no active teammates)"
        rows = []
        for teammate in sorted(active_teammates.values(), key=lambda item: item.name):
            rows.append(
                json.dumps(
                    {
                        "name": teammate.name,
                        "role": teammate.role,
                        "status": teammate.status,
                        "created_at": teammate.created_at,
                        "last_seen_at": teammate.last_seen_at,
                        "error": teammate.error,
                        "current_cwd": teammate.current_cwd,
                        "current_worktree": teammate.current_worktree,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
    return "\n".join(rows)


def active_teammate_count() -> int:
    """Return the number of teammates still capable of doing work."""
    with _team_lock:
        return sum(
            1
            for teammate in active_teammates.values()
            if teammate.status in {"running", "idle"}
        )


def _set_teammate_state(name: str, status: str, error: Optional[str] = None) -> None:
    """Update teammate status under one lock so status snapshots stay coherent."""
    with _team_lock:
        state = active_teammates.get(name)
        if state:
            state.status = status
            state.last_seen_at = time.time()
            state.error = error
    try:
        # Durable lifecycle state lets s17 inspect teammate status even though
        # the Python thread itself still lives only in memory.
        if status in {"running", "idle", "shutting_down"}:
            TASK_STORE.set_agent_state(name, status, error=error)
    except Exception as exc:
        print(f"\033[90m[team] could not persist teammate state for {name}: {exc}\033[0m")
    agent = TASK_STORE.get_agent(name)
    emit_domain_event("agent.status", {
        "agent_id": name,
        "agent_kind": "teammate",
        "status": status,
        "task_id": agent.current_task_id if agent else None,
    })


def _set_teammate_cwd(
    name: str,
    cwd: Path | str,
    worktree_name: Optional[str] = None,
) -> None:
    """Update a teammate's runtime filesystem base directory."""
    with _team_lock:
        state = active_teammates.get(name)
        if state:
            state.current_cwd = str(Path(cwd).resolve())
            state.current_worktree = worktree_name
            state.last_seen_at = time.time()


def _reset_teammate_cwd(name: str) -> None:
    """Return a teammate to the project root after finishing/releasing work."""
    _set_teammate_cwd(name, WORKDIR, None)


def _get_teammate_cwd(name: str) -> str:
    """Read the current filesystem base dir for one teammate."""
    with _team_lock:
        state = active_teammates.get(name)
        return state.current_cwd if state else str(WORKDIR)


def _set_teammate_cwd_for_task(name: str, task_id: Optional[str]) -> None:
    """Point a teammate at the worktree bound to a claimed task, if present."""
    if not task_id:
        _reset_teammate_cwd(name)
        return
    try:
        worktree = WORKTREE_STORE.get_task_worktree(task_id)
    except Exception as exc:
        print(f"\033[90m[team] could not read worktree for {task_id}: {exc}\033[0m")
        _reset_teammate_cwd(name)
        return
    if worktree:
        _set_teammate_cwd(name, worktree.path, worktree.worktree_name)
    else:
        _reset_teammate_cwd(name)


def _stop_teammate_state(
    name: str,
    status: str = "done",
    error: Optional[str] = None,
) -> None:
    """Mark a teammate terminal and release its current task atomically."""
    with _team_lock:
        state = active_teammates.get(name)
        if state:
            state.status = status
            state.last_seen_at = time.time()
            state.error = error
            state.current_cwd = str(WORKDIR)
            state.current_worktree = None
    failed_task_id = None
    try:
        # A failed teammate is a genuine task failure when it still owns work.
        # The store performs this transition atomically before the terminal
        # teammate state is persisted.
        if status == "failed":
            failed_task_id = TASK_STORE.fail_current_task(name, error or "teammate failed")
        TASK_STORE.stop_agent(name, final_status=status, error=error, release_task=True)
    except Exception as exc:
        print(f"\033[90m[team] could not stop teammate state for {name}: {exc}\033[0m")
    if failed_task_id:
        task = TASK_STORE.get_task(failed_task_id)
        emit_domain_event("task.failed", {
            "task_id": failed_task_id,
            "owner": name,
            "status": task.status if task else "failed",
            "blocked_by": task.blockedBy if task else [],
        })
    emit_domain_event("agent.completed", {
        "agent_id": name,
        "agent_kind": "teammate",
        "status": status,
        "error": error if status == "failed" else None,
    })


def _teammate_system(name: str, role: str) -> str:
    """Build a compact, role-specific system prompt for a teammate."""
    return (
        f"You are '{name}', a teammate agent working at {WORKDIR}.\n"
        f"Role: {role}\n\n"
        "Complete the delegated work directly with your available tools. "
        "Use check_inbox to receive follow-up instructions and send_message to "
        "report progress or final results to lead. When you claim an autonomous "
        "task with a bound worktree, your file tools run inside that isolated "
        "worktree. Call complete_task only after the work is actually finished. "
        "Do not create other teammates."
    )


def _execute_teammate_tool(name: str, block) -> str:
    """Execute a teammate tool call with the teammate as message sender."""
    cwd = _get_teammate_cwd(name)
    if block.name == "bash":
        return run_bash(cwd=cwd, **block.input)
    if block.name == "read_file":
        return run_read(base_dir=cwd, **block.input)
    if block.name == "write_file":
        return run_write(base_dir=cwd, **block.input)
    if block.name == "edit_file":
        return run_edit(base_dir=cwd, **block.input)
    if block.name == "glob":
        return run_glob(base_dir=cwd, **block.input)
    if block.name == "send_message":
        return run_send_message(from_agent=name, **block.input)
    if block.name == "check_inbox":
        # Teammates may only consume their own inbox; this avoids one teammate
        # accidentally eating Lead or another teammate's messages.
        requested_agent = str(block.input.get("agent", name))
        if requested_agent != name:
            return "Error: teammates can only check their own inbox"
        return run_check_inbox(agent=name)
    if block.name == "submit_plan":
        return submit_plan(from_agent=name, **block.input)
    if block.name == "list_tasks":
        return list_tasks(**block.input)
    if block.name == "claim_task":
        # A teammate can only claim work for itself. This keeps task ownership
        # aligned with the durable team_agents lifecycle row.
        requested_owner = str(block.input.get("owner", name))
        if requested_owner != name:
            return "Error: teammates can only claim tasks for themselves"
        output = claim_task(task_id=block.input["task_id"], owner=name)
        if output.startswith("Claimed"):
            _set_teammate_cwd_for_task(name, block.input["task_id"])
        return output
    if block.name == "complete_task":
        requested_owner = block.input.get("owner")
        if requested_owner and str(requested_owner) != name:
            return "Error: teammates can only complete tasks they own"
        output = complete_task(task_id=block.input["task_id"], owner=name)
        if output.startswith("Completed"):
            _reset_teammate_cwd(name)
        return output
    return f"Error: Unknown teammate tool: {block.name}"


def _wait_for_teammate_work(name: str, messages: list) -> str:
    """Idle until this teammate receives inbox work, board work, or shutdown.

    s17 extends the s16 idle loop: inbox still has priority, but if no message
    arrives the teammate scans the SQLite task board and atomically claims an
    available task.
    """
    _set_teammate_state(name, "idle")
    _send_team_message(
        name,
        TEAM_AGENT_ID,
        f"{name} is idle and waiting for more work.",
        msg_type="idle_notification",
    )

    result = idle_poll_for_work(name, messages)
    if result.action == "shutdown":
        _set_teammate_state(name, "shutting_down")
        return "shutdown"
    if result.action == "work":
        _set_teammate_cwd_for_task(name, result.task_id)
        _set_teammate_state(name, "running")
        return "work"
    _set_teammate_state(name, "shutting_down")
    return "timeout"


def _run_teammate(
    name: str,
    role: str,
    prompt: str,
    event_context: Optional[DomainEventContext] = None,
) -> None:
    """Rebind the lead run's lightweight context inside this new thread."""
    with activate_domain_events(event_context):
        _run_teammate_with_context(name, role, prompt)


def _run_teammate_with_context(name: str, role: str, prompt: str) -> None:
    """Run the teammate's independent, bounded agent loop."""
    # Import the API client only when a teammate actually starts. This keeps
    # no-API unit tests able to import the team/message-bus modules.
    from ..runtime.client import get_client

    messages = [{"role": "user", "content": prompt}]
    system = _teammate_system(name, role)
    last_text = "(teammate did not finish)"

    try:
        turns = 0
        while turns < TEAMMATE_MAX_TURNS:
            turns += 1
            _set_teammate_state(name, "running")
            inbox = consume_teammate_inbox(name, messages)
            if inbox.shutdown_requested:
                last_text = f"Teammate {name} shut down gracefully."
                _stop_teammate_state(name, "done")
                break

            response = get_client().messages.create(
                model=MODEL,
                system=system,
                messages=messages[-20:],
                tools=TEAMMATE_TOOLS,
                max_tokens=8000,
            )

            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                last_text = extract_text(response.content)
                idle_action = _wait_for_teammate_work(name, messages)
                if idle_action == "work":
                    continue
                if idle_action == "timeout":
                    last_text = f"Teammate {name} shut down after idle timeout."
                    _stop_teammate_state(name, "done")
                    break
                if idle_action == "shutdown":
                    last_text = f"Teammate {name} shut down gracefully."
                    _stop_teammate_state(name, "done")
                    break

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    output = str(blocked)
                    is_error = True
                else:
                    try:
                        output = _execute_teammate_tool(name, block)
                    except TypeError as exc:
                        output = f"Error: Invalid teammate tool input for {block.name}: {exc}"
                    trigger_hooks("PostToolUse", block, output)
                    is_error = output.startswith("Error:")

                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                    **({"is_error": True} if is_error else {}),
                })

            messages.append({"role": "user", "content": results})
            acknowledge_staged_inbox_messages()
        else:
            last_text = extract_text(messages[-1].get("content", ""))
            last_text = (
                f"Teammate {name} stopped after {TEAMMATE_MAX_TURNS} turns. "
                f"Last result:\n{last_text}"
            )
            _stop_teammate_state(name, "done")

        _send_team_message(name, TEAM_AGENT_ID, last_text, msg_type="result")
    except Exception as exc:
        _stop_teammate_state(name, "failed", error=str(exc))
        _send_team_message(
            name,
            TEAM_AGENT_ID,
            f"Teammate {name} failed: {exc}",
            msg_type="result",
        )


def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """Create a named teammate thread with its own context and SQLite inbox."""
    try:
        # Reuse the bus validator so thread names obey the same prompt-safe id
        # rules as message sender/recipient names.
        BUS._validate_agent_id(name)
    except ValueError as exc:
        return f"Error: {exc}"

    normalized_name = str(name).strip()
    normalized_role = str(role).strip() or "generalist"
    initial_prompt = str(prompt).strip()
    if not initial_prompt:
        return "Error: teammate prompt must not be empty"

    event_context = current_domain_event_context()
    with _team_lock:
        running_count = sum(
            1
            for teammate in active_teammates.values()
            if teammate.status in {"running", "idle"}
        )
        if normalized_name in active_teammates and active_teammates[normalized_name].status in {
            "running",
            "idle",
        }:
            return f"Error: teammate already running: {normalized_name}"
        if running_count >= MAX_TEAMMATES:
            return f"Error: too many teammates running (max {MAX_TEAMMATES})"

        try:
            TASK_STORE.register_agent(normalized_name, normalized_role, status="running")
        except Exception as exc:
            return f"Error: could not register teammate {normalized_name}: {exc}"

        state = TeammateState(
            name=normalized_name,
            role=normalized_role,
            status="running",
            created_at=time.time(),
            last_seen_at=time.time(),
        )
        thread = threading.Thread(
            target=_run_teammate,
            args=(normalized_name, normalized_role, initial_prompt, event_context),
            daemon=True,
            name=f"agent-team-{normalized_name}",
        )
        state.thread = thread
        active_teammates[normalized_name] = state

    emit_domain_event("agent.spawned", {
        "agent_id": normalized_name,
        "agent_kind": "teammate",
        "role": normalized_role,
        "status": "running",
    })
    # Publish the durable registration edge before the new thread can emit a
    # status/complete event. This keeps the run timeline causally ordered.
    thread.start()
    return (
        f"Spawned teammate {normalized_name} as {normalized_role}. "
        "Use send_message for follow-up and check_inbox for results."
    )
