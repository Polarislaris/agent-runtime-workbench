"""Cron scheduler for time-based work.

s14 separates scheduling from execution:
- Scheduler thread checks cron expressions and enqueues due jobs.
- Queue processor thread waits for the Agent to be idle, then wakes it.
- agent_loop drains queued prompts and remains the consumer that decides tools.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from queue import Empty, Queue
import re
import secrets
import threading
import time
from typing import Callable, Optional

from ..config import (
    CRON_QUEUE_PROCESSOR_INTERVAL_SECONDS,
    CRON_SCHEDULER_INTERVAL_SECONDS,
    SCHEDULED_TASKS_FILE,
)


@dataclass
class CronJob:
    """A scheduled prompt producer."""

    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool


@dataclass(frozen=True)
class CronNotification:
    """A due job transferred to the loop without losing its durable id."""

    job_id: str
    prompt: str


_scheduled_jobs: dict[str, CronJob] = {}
_last_fired: dict[str, str] = {}
_cron_queue: Queue[CronJob] = Queue()
_cron_lock = threading.Lock()

# Shared Agent lock: manual user turns and scheduled turns both acquire this so
# only one agent_loop invocation runs at a time.
_agent_lock = threading.Lock()

_scheduler_started = False
_queue_processor_started = False


def _new_cron_id() -> str:
    """Generate a short readable schedule id."""
    return f"cron_{int(time.time())}_{secrets.token_hex(2)}"


def _parse_int(value: str) -> Optional[int]:
    """Parse a field integer; invalid fields return None for validation errors."""
    try:
        return int(value)
    except ValueError:
        return None


def _cron_field_matches(field: str, value: int, min_value: int, max_value: int) -> bool:
    """Match one cron field supporting *, */N, N, N-M, and comma lists."""
    for part in field.split(","):
        part = part.strip()
        if not part:
            return False

        if part == "*":
            return True

        if part.startswith("*/"):
            step = _parse_int(part[2:])
            if not step or step <= 0:
                return False
            return value % step == 0

        if "-" in part:
            start_raw, end_raw = part.split("-", 1)
            start = _parse_int(start_raw)
            end = _parse_int(end_raw)
            if start is None or end is None or start > end:
                return False
            if start <= value <= end:
                return True
            continue

        exact = _parse_int(part)
        if exact is None:
            return False
        if exact < min_value or exact > max_value:
            return False
        # Cron allows both 0 and 7 for Sunday. Internally cron_matches maps
        # Sunday to 0, so treat field value 7 as a Sunday match too.
        if max_value == 7 and value == 0 and exact == 7:
            return True
        if value == exact:
            return True

    return False


def validate_cron(cron: str) -> Optional[str]:
    """Validate a five-field cron expression before storing it."""
    fields = str(cron or "").strip().split()
    if len(fields) != 5:
        return "cron must have exactly five fields: minute hour day month weekday"

    now = datetime.now()
    checks = [
        (fields[0], now.minute, 0, 59, "minute"),
        (fields[1], now.hour, 0, 23, "hour"),
        (fields[2], now.day, 1, 31, "day-of-month"),
        (fields[3], now.month, 1, 12, "month"),
        (fields[4], (now.weekday() + 1) % 7, 0, 7, "weekday"),
    ]
    for field, sample, min_value, max_value, name in checks:
        if not _cron_field_matches(field, sample, min_value, max_value):
            # A sample mismatch is normal for valid cron, so run structural checks
            # by testing every allowed value instead of relying on current time.
            if not any(
                _cron_field_matches(field, value, min_value, max_value)
                for value in range(min_value, max_value + 1)
            ):
                return f"invalid {name} cron field: {field}"
    return None


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """Return True when a datetime matches a five-field cron expression."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False

    minute, hour, dom, month, dow = fields
    dow_value = (dt.weekday() + 1) % 7

    minute_ok = _cron_field_matches(minute, dt.minute, 0, 59)
    hour_ok = _cron_field_matches(hour, dt.hour, 0, 23)
    month_ok = _cron_field_matches(month, dt.month, 1, 12)
    dom_ok = _cron_field_matches(dom, dt.day, 1, 31)
    dow_ok = _cron_field_matches(dow, dow_value, 0, 7)
    if not (minute_ok and hour_ok and month_ok):
        return False

    dom_unconstrained = dom == "*"
    dow_unconstrained = dow == "*"
    if dom_unconstrained and dow_unconstrained:
        return True
    if dom_unconstrained:
        return dow_ok
    if dow_unconstrained:
        return dom_ok
    return dom_ok or dow_ok


def _job_from_dict(data: dict) -> CronJob:
    """Build a CronJob from durable JSON."""
    return CronJob(
        id=str(data["id"]),
        cron=str(data["cron"]),
        prompt=str(data["prompt"]),
        recurring=bool(data.get("recurring", True)),
        durable=bool(data.get("durable", True)),
    )


def _save_durable_jobs() -> None:
    """Persist durable cron definitions."""
    durable = [
        asdict(job)
        for job in sorted(_scheduled_jobs.values(), key=lambda item: item.id)
        if job.durable
    ]
    SCHEDULED_TASKS_FILE.write_text(
        json.dumps(durable, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_durable_jobs() -> None:
    """Load durable cron definitions, skipping invalid entries."""
    if not SCHEDULED_TASKS_FILE.is_file():
        return

    try:
        raw_jobs = json.loads(SCHEDULED_TASKS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"\033[90m[cron] failed to load durable jobs: {e}\033[0m")
        return

    with _cron_lock:
        for raw in raw_jobs:
            try:
                job = _job_from_dict(raw)
                if validate_cron(job.cron):
                    continue
                _scheduled_jobs[job.id] = job
            except Exception as e:
                print(f"\033[90m[cron] skipped malformed job: {e}\033[0m")


def schedule_cron(
    cron: str,
    prompt: str,
    recurring: bool = True,
    durable: bool = True,
) -> str:
    """Tool handler: register a scheduled prompt."""
    cron = str(cron or "").strip()
    prompt = str(prompt or "").strip()
    if not prompt:
        return "Error: prompt must not be empty"

    err = validate_cron(cron)
    if err:
        return f"Error: {err}"

    job = CronJob(
        id=_new_cron_id(),
        cron=cron,
        prompt=prompt,
        recurring=bool(recurring),
        durable=bool(durable),
    )
    with _cron_lock:
        _scheduled_jobs[job.id] = job
        if job.durable:
            _save_durable_jobs()

    return json.dumps(asdict(job), ensure_ascii=False, indent=2)


def list_crons() -> str:
    """Tool handler: list scheduled cron jobs."""
    with _cron_lock:
        jobs = sorted(_scheduled_jobs.values(), key=lambda item: item.id)
    if not jobs:
        return "(no cron jobs)"

    lines = []
    for job in jobs:
        mode = "recurring" if job.recurring else "one-shot"
        durable = "durable" if job.durable else "session"
        lines.append(f"- {job.id} [{mode}, {durable}] {job.cron}: {job.prompt}")
    return "\n".join(lines)


def cancel_cron(job_id: str) -> str:
    """Tool handler: cancel one scheduled cron job."""
    job_id = str(job_id or "").strip()
    with _cron_lock:
        job = _scheduled_jobs.pop(job_id, None)
        if job and job.durable:
            _save_durable_jobs()

    if not job:
        return f"Error: cron job not found: {job_id}"
    return f"Cancelled {job.id}"


def scheduled_cron_count() -> int:
    """Return current scheduled job count for prompt context."""
    with _cron_lock:
        return len(_scheduled_jobs)


def queued_cron_count() -> int:
    """Return queued fired-job count for prompt context."""
    return _cron_queue.qsize()


def _scheduler_loop() -> None:
    """Daemon scheduler: check time and enqueue due jobs."""
    while True:
        time.sleep(CRON_SCHEDULER_INTERVAL_SECONDS)
        now = datetime.now()
        minute_marker = now.strftime("%Y-%m-%d %H:%M")

        with _cron_lock:
            jobs = list(_scheduled_jobs.values())

        for job in jobs:
            try:
                if not cron_matches(job.cron, now):
                    continue
                with _cron_lock:
                    if _last_fired.get(job.id) == minute_marker:
                        continue
                    _last_fired[job.id] = minute_marker
                    _cron_queue.put(job)
                    if not job.recurring:
                        _scheduled_jobs.pop(job.id, None)
                        if job.durable:
                            _save_durable_jobs()
                print(f"\033[90m[cron] fired {job.id}\033[0m")
            except Exception as e:
                print(f"\033[90m[cron error] {job.id}: {e}\033[0m")


def _drain_cron_queue() -> list[CronJob]:
    """Drain currently fired jobs for the agent loop to inject."""
    jobs = []
    while True:
        try:
            jobs.append(_cron_queue.get_nowait())
        except Empty:
            break
    return jobs


def collect_cron_notifications() -> list[CronNotification]:
    """Drain due cron jobs and keep their ids for observable run injection."""
    return [
        CronNotification(job_id=job.id, prompt=job.prompt)
        for job in _drain_cron_queue()
    ]


def run_agent_turn_locked(
    history: list,
    agent_runner: Callable[[list], None],
    print_final_text: Optional[Callable[[list], None]] = None,
    blocking: bool = True,
) -> bool:
    """Run one agent turn only when the shared Agent lock can be acquired."""
    if not _agent_lock.acquire(blocking=blocking):
        return False
    try:
        agent_runner(history)
        if print_final_text is not None and history:
            print_final_text(history)
        return True
    finally:
        _agent_lock.release()


def _queue_processor_loop(
    history: list,
    agent_runner: Callable[[list], None],
    print_final_text: Optional[Callable[[list], None]],
) -> None:
    """Daemon processor: wake the Agent for queued cron work when it is idle."""
    while True:
        time.sleep(CRON_QUEUE_PROCESSOR_INTERVAL_SECONDS)
        if _cron_queue.empty():
            continue
        if not _agent_lock.acquire(blocking=False):
            continue

        try:
            # Leave queue draining and history mutation to agent_loop.  That
            # gives cron the same append-then-ack injection boundary as other
            # runtime notifications.
            print("\033[90m[queue processor] waking agent for cron work\033[0m")
            agent_runner(history)
            if print_final_text is not None and history:
                print_final_text(history)
        finally:
            _agent_lock.release()


def start_cron_services(
    history: list,
    agent_runner: Callable[[list], None],
    print_final_text: Optional[Callable[[list], None]] = None,
) -> None:
    """Start scheduler and queue processor daemon threads once."""
    global _scheduler_started, _queue_processor_started

    load_durable_jobs()

    if not _scheduler_started:
        threading.Thread(target=_scheduler_loop, daemon=True, name="cron-scheduler").start()
        _scheduler_started = True

    if not _queue_processor_started:
        threading.Thread(
            target=_queue_processor_loop,
            args=(history, agent_runner, print_final_text),
            daemon=True,
            name="cron-queue-processor",
        ).start()
        _queue_processor_started = True
