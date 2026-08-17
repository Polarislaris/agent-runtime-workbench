"""Background execution for slow, independent tool calls.

s13 adds a small asynchronous layer around tool execution. It does not decide
project dependency correctness; task_system.blockedBy and the parent agent's
reasoning still decide whether a follow-up step can safely run.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from queue import Empty, Queue
import threading
import time
from typing import Callable, Optional

from ..config import BACKGROUND_RESULT_PREVIEW_CHARS, MAX_BACKGROUND_TASKS


SLOW_COMMAND_KEYWORDS = [
    "install",
    "build",
    "test",
    "deploy",
    "compile",
    "docker build",
    "pip install",
    "npm install",
    "cargo build",
    "pytest",
    "make",
]


@dataclass
class BackgroundTask:
    """In-memory lifecycle record for one background tool execution."""

    id: str
    tool_use_id: str
    tool_name: str
    command: str
    status: str
    started_at: float
    completed_at: Optional[float] = None
    output: Optional[str] = None
    error: Optional[str] = None


@dataclass
class BackgroundNotification:
    """Completion event delivered from worker threads to the agent loop."""

    task_id: str
    text: str


_bg_counter = 0
_background_tasks: dict[str, BackgroundTask] = {}
_background_lock = threading.Lock()

# ThreadPoolExecutor owns the worker threads. Queue owns completion delivery.
# The dict remains the source of truth for task state until loop.py confirms the
# notification was injected into messages and acknowledges it.
_executor = ThreadPoolExecutor(
    max_workers=MAX_BACKGROUND_TASKS,
    thread_name_prefix="agent-bg",
)
_completion_queue: Queue[BackgroundNotification] = Queue()


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """Heuristic fallback for bash commands likely to run for a while."""
    if tool_name != "bash":
        return False

    command = str(tool_input.get("command", "")).lower()
    return any(keyword in command for keyword in SLOW_COMMAND_KEYWORDS)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """Return True when a tool call should be started asynchronously.

    Explicit run_in_background is the primary signal. The keyword heuristic is a
    convenience fallback only; prompt policy still tells the model not to run
    dependent follow-up work until a notification arrives.
    """
    if tool_name != "bash":
        return False
    if bool(tool_input.get("run_in_background")):
        return True
    return is_slow_operation(tool_name, tool_input)


def running_background_count() -> int:
    """Count currently running background tasks."""
    with _background_lock:
        return sum(1 for task in _background_tasks.values() if task.status == "running")


def _next_background_id() -> str:
    """Allocate a readable in-memory background id."""
    global _bg_counter
    _bg_counter += 1
    return f"bg_{_bg_counter:04d}"


def start_background_task(
    block,
    execute_tool: Callable[[object], str],
    *,
    on_started: Optional[Callable[[BackgroundTask], None]] = None,
    on_completed: Optional[Callable[[BackgroundTask], None]] = None,
) -> str:
    """Submit one tool call and optionally report its real lifecycle edges.

    The callbacks carry a task snapshot, not the lead message list.  This lets
    the Web runtime observe background work safely from an executor thread.
    """
    with _background_lock:
        running_count = sum(
            1 for task in _background_tasks.values() if task.status == "running"
        )
        if running_count >= MAX_BACKGROUND_TASKS:
            raise RuntimeError(
                f"Too many background tasks running (max {MAX_BACKGROUND_TASKS})"
            )

        bg_id = _next_background_id()
        _background_tasks[bg_id] = BackgroundTask(
            id=bg_id,
            tool_use_id=str(block.id),
            tool_name=str(block.name),
            command=str(block.input.get("command", "")),
            status="running",
            started_at=time.time(),
        )
        task = _background_tasks[bg_id]

    if on_started is not None:
        on_started(task)

    def worker() -> None:
        try:
            output = execute_tool(block)
            status = "completed"
            error = None
        except Exception as exc:
            output = ""
            status = "failed"
            error = str(exc)

        with _background_lock:
            task = _background_tasks.get(bg_id)
            if task is None:
                return
            task.status = status
            task.completed_at = time.time()
            task.output = output
            task.error = error
            notification = _format_notification(task)

        if on_completed is not None:
            on_completed(task)

        # Queue delivery happens after state is updated. The task remains in the
        # dict until loop.py has appended this notification into messages.
        _completion_queue.put(BackgroundNotification(task_id=bg_id, text=notification))

    _executor.submit(worker)
    return bg_id


def _format_notification(task: BackgroundTask) -> str:
    """Format one completed task as a task_notification message."""
    output = task.output or task.error or ""
    preview = output[:BACKGROUND_RESULT_PREVIEW_CHARS]
    return (
        "<task_notification>\n"
        f"  <task_id>{task.id}</task_id>\n"
        f"  <status>{task.status}</status>\n"
        f"  <command>{task.command}</command>\n"
        f"  <summary>{preview}</summary>\n"
        "</task_notification>"
    )


def collect_background_results() -> list[BackgroundNotification]:
    """Drain completed background notifications without deleting task state."""
    notifications = []
    while True:
        try:
            notifications.append(_completion_queue.get_nowait())
        except Empty:
            break
    return notifications


def acknowledge_background_notifications(notifications: list[BackgroundNotification]) -> None:
    """Delete completed task state after notifications have entered messages."""
    with _background_lock:
        for notification in notifications:
            _background_tasks.pop(notification.task_id, None)
