"""In-memory lifecycle manager for one observable Web Agent run at a time.

This is deliberately an MVP boundary: runs, events, and permission waiters are
lost when the API process restarts. Durable history and multi-run scheduling
belong to the stable version described in ``frontend/DESIGN.md``.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from queue import Queue
from threading import Event, RLock
from typing import Callable, Literal
from uuid import uuid4

from ..config import WEB_PERMISSION_TIMEOUT_SECONDS
from ..database.runs import RunStore, StoredRun, StoredRunNotFoundError
from ..runtime.event_payloads import summarize_tool_input
from ..runtime.events import (
    AgentLoopResult,
    CancellationToken,
    EventSink,
    MessageJournal,
    PermissionDecision,
    RunEvent,
    RuntimeContext,
)
from ..runtime.loop import agent_loop
from ..runtime.messages import serialize_message
from ..tooling.hooks import collect_hook_messages
from .models import RunSnapshot, RunStatus


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
ACTIVE_STATUSES = {"queued", "running", "waiting_permission"}
_STATUS_BY_EVENT = {
    "run.started": "running",
    "permission.requested": "waiting_permission",
    "permission.resolved": "running",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
    "run.interrupted": "interrupted",
}
Clock = Callable[[], datetime]
RunIdFactory = Callable[[], str]
EventIdFactory = Callable[[], str]
AgentRunner = Callable[[list, RuntimeContext], AgentLoopResult]
HookCollector = Callable[..., list[dict]]
PermissionResolution = Literal["user", "timeout", "cancelled", "run_terminated"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_run_id() -> str:
    return f"run_{uuid4().hex}"


def _new_event_id() -> str:
    return f"evt_{uuid4().hex}"


def _isoformat_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("run timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class RunManagerError(RuntimeError):
    """Base error mapped to HTTP responses in Step 6."""


class RunNotFoundError(RunManagerError):
    pass


class ActiveRunError(RunManagerError):
    pass


class PermissionNotFoundError(RunManagerError):
    pass


class PermissionAlreadyResolvedError(RunManagerError):
    pass


class InvalidPermissionDecisionError(RunManagerError):
    pass


class RunStoreNotAttachedError(RunManagerError):
    """Raised when a manager is used before the API lifecycle supplies storage."""

    pass


@dataclass
class PendingPermission:
    """One browser decision currently awaited by the Agent worker thread."""

    id: str
    tool_name: str
    args_preview: dict
    reason: str
    resolved: Event = field(default_factory=Event)
    decision: PermissionDecision | None = None
    resolution: PermissionResolution | None = None


@dataclass
class RunState:
    """Mutable state protected by one re-entrant lock per run."""

    id: str
    title: str
    status: RunStatus
    messages: list
    started_at: str
    # Every SSE connection receives an independent queue.  A single shared
    # queue would make two browser tabs steal events from each other and makes
    # it impossible to safely subscribe before durable catch-up.
    subscribers: dict[str, Queue[RunEvent]] = field(default_factory=dict)
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    permissions: dict[str, PendingPermission] = field(default_factory=dict)
    completed_at: str | None = None
    error: str | None = None
    worker: Future[AgentLoopResult] | None = None
    lock: RLock = field(default_factory=RLock, repr=False)


class DurableRunEventSink(EventSink):
    """Persist events before publishing them to the live SSE queue.

    One state lock covers the SQLite commit and queue put. This prevents a
    background worker from publishing sequence 2 before another worker has
    queued already-committed sequence 1.
    """

    def __init__(
        self,
        state: RunState,
        store: RunStore,
        *,
        clock: Clock,
        event_id_factory: EventIdFactory,
    ) -> None:
        self._state = state
        self._store = store
        self._clock = clock
        self._event_id_factory = event_id_factory

    def emit(self, event_type: str, payload: dict) -> None:
        with self._state.lock:
            event = self._store.append_next_event(
                run_id=self._state.id,
                event_type=event_type,
                payload=payload,
                event_id_factory=self._event_id_factory,
                created_at=_isoformat_utc(self._clock()),
            )
            next_status = _STATUS_BY_EVENT.get(event_type)
            if next_status is not None:
                self._state.status = next_status
                if next_status in TERMINAL_STATUSES:
                    self._state.completed_at = event.created_at
                    self._state.error = (
                        str(payload.get("error", "")) or None
                        if next_status == "failed"
                        else None
                    )
            # The SQLite commit above is the source of truth.  Publishing to
            # per-connection queues happens only afterwards, while the same
            # lock also protects subscriber registration in ``subscribe``.
            for event_queue in self._state.subscribers.values():
                event_queue.put(event)


class RunMessageJournal(MessageJournal):
    """Serialize new conversation messages into the durable run event log."""

    def __init__(self, store: RunStore, run_id: str) -> None:
        self._store = store
        self._run_id = run_id

    def append(self, message: object) -> None:
        if not isinstance(message, dict):
            raise TypeError("runtime messages must be dictionaries")
        serialized = serialize_message(message)
        self._store.append_message(
            self._run_id,
            str(serialized.get("role", "")),
            serialized.get("content"),
        )


class WebPermissionProvider:
    """Bridge a synchronous tool hook to an in-memory browser decision."""

    def __init__(
        self,
        state: RunState,
        events: EventSink,
        store: RunStore,
        *,
        timeout_seconds: float = WEB_PERMISSION_TIMEOUT_SECONDS,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._state = state
        self._events = events
        self._store = store
        self._timeout_seconds = max(0.0, timeout_seconds)
        self._request_id_factory = request_id_factory or (
            lambda: f"perm_{uuid4().hex}"
        )

    def decide(
        self,
        tool_name: str,
        args: dict,
        reason: str,
    ) -> PermissionDecision:
        pending = PendingPermission(
            id=self._request_id_factory(),
            tool_name=str(tool_name),
            args_preview=summarize_tool_input(tool_name, args),
            reason=str(reason),
        )
        with self._state.lock:
            # Recheck under the same lock used by cancel_run. Without this
            # boundary, cancellation could happen between the initial check
            # and registration, leaving a new waiter asleep until timeout.
            if self._state.cancellation.is_cancelled():
                return "deny"
            # Store the pending approval before publishing its event. A browser
            # can therefore refresh immediately and still find the request in
            # the durable run history.
            self._store.create_permission_request(
                pending.id,
                self._state.id,
                pending.tool_name,
                pending.args_preview,
                pending.reason,
            )
            self._state.permissions[pending.id] = pending
            self._state.status = "waiting_permission"
            self._events.emit("permission.requested", {
                "request_id": pending.id,
                "tool": pending.tool_name,
                "args_preview": pending.args_preview,
                "reason": pending.reason,
            })

        was_resolved = pending.resolved.wait(self._timeout_seconds)

        with self._state.lock:
            if self._state.cancellation.is_cancelled():
                pending.decision = "deny"
                pending.resolution = "cancelled"
                self._store.expire_pending_permissions(self._state.id)
            elif not was_resolved or pending.decision is None:
                pending.decision = "deny"
                pending.resolution = "timeout"
                self._store.expire_pending_permissions(self._state.id)
            elif pending.resolution is None:
                pending.resolution = "user"

            if self._state.status == "waiting_permission":
                self._state.status = "running"

            self._events.emit("permission.resolved", {
                "request_id": pending.id,
                "tool": pending.tool_name,
                "decision": pending.decision,
                "resolution": pending.resolution,
            })
            return pending.decision


class RunManager:
    """Own run state, one worker, event delivery, cancellation, and approvals."""

    def __init__(
        self,
        *,
        agent_runner: AgentRunner = agent_loop,
        hook_collector: HookCollector = collect_hook_messages,
        max_workers: int = 1,
        permission_timeout_seconds: float = WEB_PERMISSION_TIMEOUT_SECONDS,
        clock: Clock = _utc_now,
        run_id_factory: RunIdFactory = _new_run_id,
        event_id_factory: EventIdFactory = _new_event_id,
        permission_id_factory: Callable[[], str] | None = None,
        run_store: RunStore | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self._agent_runner = agent_runner
        self._hook_collector = hook_collector
        self._permission_timeout_seconds = max(0.0, permission_timeout_seconds)
        self._clock = clock
        self._run_id_factory = run_id_factory
        self._event_id_factory = event_id_factory
        self._permission_id_factory = permission_id_factory
        self._run_store = run_store
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="agent-run",
        )
        self._runs: dict[str, RunState] = {}
        self._lock = RLock()

    def attach_run_store(self, run_store: RunStore) -> None:
        """Attach lifecycle-owned durable storage before the first Web run."""
        with self._lock:
            if self._runs:
                raise RunManagerError("cannot replace run storage while runs are active")
            self._run_store = run_store

    def _store(self) -> RunStore:
        """Return the lifecycle-owned store instead of falling back to hidden state."""
        if self._run_store is None:
            raise RunStoreNotAttachedError(
                "RunManager requires a RunStore from the API lifecycle"
            )
        return self._run_store

    def create_run(self, prompt: str) -> RunSnapshot:
        normalized_prompt = str(prompt or "").strip()
        if not normalized_prompt:
            raise ValueError("prompt must not be empty")

        store = self._store()
        with self._lock:
            if any(self._is_active(state) for state in self._runs.values()):
                raise ActiveRunError("another Agent run is already active")

            run_id = str(self._run_id_factory()).strip()
            if not run_id or run_id in self._runs:
                raise RunManagerError("run id factory returned an invalid or duplicate id")

            messages = [{"role": "user", "content": normalized_prompt}]
            messages.extend(
                self._hook_collector("UserPromptSubmit", normalized_prompt)
            )
            started_at = _isoformat_utc(self._clock())
            # Initial user input and hook context must exist before a worker can
            # emit run.started. This transaction makes a restarted API able to
            # explain even a run that dies before the model request begins.
            store.create_run(
                run_id,
                self._title_from_prompt(normalized_prompt),
                started_at=started_at,
                initial_messages=[serialize_message(message) for message in messages],
            )
            state = RunState(
                id=run_id,
                title=self._title_from_prompt(normalized_prompt),
                status="queued",
                messages=messages,
                started_at=started_at,
            )
            self._runs[run_id] = state

            events = DurableRunEventSink(
                state,
                store,
                clock=self._clock,
                event_id_factory=self._event_id_factory,
            )
            permissions = WebPermissionProvider(
                state,
                events,
                store,
                timeout_seconds=self._permission_timeout_seconds,
                request_id_factory=self._permission_id_factory,
            )
            runtime = RuntimeContext(
                run_id=run_id,
                events=events,
                message_journal=RunMessageJournal(store, run_id),
                permissions=permissions,
                cancellation=state.cancellation,
            )
            try:
                state.worker = self._executor.submit(
                    self._execute_run,
                    state,
                    runtime,
                    events,
                )
            except Exception:
                del self._runs[run_id]
                raise

        return self.snapshot(state)

    def get_run(self, run_id: str) -> RunState:
        with self._lock:
            state = self._runs.get(str(run_id))
        if state is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        return state

    def list_runs(
        self,
        *,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> list[RunSnapshot]:
        return [
            self._snapshot_from_record(record)
            for record in self._store().list_runs(
                status=status,
                cursor=cursor,
                limit=limit,
            )
        ]

    def cancel_run(self, run_id: str) -> RunSnapshot:
        state = self.get_run(run_id)
        with state.lock:
            if state.status not in TERMINAL_STATUSES:
                state.cancellation.cancel()
                self._deny_pending_permissions(state, resolution="cancelled")
        return self.snapshot(state)

    def subscribe(self, run_id: str) -> tuple[str, Queue[RunEvent]]:
        """Register one SSE consumer before it performs durable catch-up.

        The caller must later call :meth:`unsubscribe`.  Registration shares
        the event sink's lock, so an event is either present in SQLite before
        catch-up starts or is queued for this subscriber after registration.
        """
        state = self.get_run(run_id)
        subscription_id = f"sub_{uuid4().hex}"
        event_queue: Queue[RunEvent] = Queue()
        with state.lock:
            state.subscribers[subscription_id] = event_queue
        return subscription_id, event_queue

    def unsubscribe(self, run_id: str, subscription_id: str) -> None:
        """Release a disconnected browser's in-memory delivery queue."""
        state = self.live_state_or_none(run_id)
        if state is None:
            return
        with state.lock:
            state.subscribers.pop(subscription_id, None)

    def resolve_permission(
        self,
        run_id: str,
        request_id: str,
        decision: PermissionDecision,
    ) -> None:
        if decision not in {"allow", "deny"}:
            raise InvalidPermissionDecisionError(
                f"invalid permission decision: {decision}"
            )

        state = self.get_run(run_id)
        with state.lock:
            pending = state.permissions.get(str(request_id))
            if pending is None:
                raise PermissionNotFoundError(
                    f"permission request not found: {request_id}"
                )
            if pending.decision is not None or pending.resolved.is_set():
                raise PermissionAlreadyResolvedError(
                    f"permission request already resolved: {request_id}"
                )
            # The database decision is committed before the worker wakes. If a
            # process dies after this line, replay still reflects the user's
            # actual approval rather than a stale pending card.
            self._store().resolve_permission_request(run_id, request_id, decision)
            pending.decision = decision
            pending.resolution = "user"
            pending.resolved.set()

    def snapshot(self, state_or_id: RunState | str) -> RunSnapshot:
        run_id = state_or_id.id if isinstance(state_or_id, RunState) else str(state_or_id)
        try:
            return self._snapshot_from_record(self._store().get_run(run_id))
        except StoredRunNotFoundError as error:
            raise RunNotFoundError(str(error)) from error

    def live_state_or_none(self, run_id: str) -> RunState | None:
        """Return an in-process state only when the run still has a live worker."""
        with self._lock:
            return self._runs.get(str(run_id))

    def durable_events(self, run_id: str, *, after_sequence: int = 0) -> list[RunEvent]:
        """Read persisted events for historical replay or later SSE catch-up."""
        try:
            return self._store().list_events(run_id, after_sequence=after_sequence)
        except StoredRunNotFoundError as error:
            raise RunNotFoundError(str(error)) from error

    def event_sequence(self, run_id: str, event_id: str) -> int | None:
        """Resolve an SSE ``Last-Event-ID`` to its durable sequence number."""
        try:
            return self._store().event_sequence(run_id, event_id)
        except StoredRunNotFoundError as error:
            raise RunNotFoundError(str(error)) from error

    def permission_history(self, run_id: str):
        """Return durable permission rows for the stable API audit endpoint."""
        try:
            return self._store().list_permission_requests(run_id)
        except StoredRunNotFoundError as error:
            raise RunNotFoundError(str(error)) from error

    def _snapshot_from_record(self, record: StoredRun) -> RunSnapshot:
        """Compose an API snapshot exclusively from durable storage."""
        store = self._store()
        messages = [
            {"role": message.role, "content": message.content}
            for message in store.list_messages(record.id)
        ]
        events = [event.to_dict() for event in store.list_events(record.id)]
        return RunSnapshot(
            id=record.id,
            title=record.title,
            status=record.status,
            messages=messages,
            events=events,
            started_at=record.started_at,
            completed_at=record.completed_at,
            error=record.error_summary,
            last_sequence=record.last_sequence,
        )

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            states = list(self._runs.values())
        for state in states:
            with state.lock:
                if state.status in ACTIVE_STATUSES:
                    state.cancellation.cancel()
                    self._deny_pending_permissions(state, resolution="run_terminated")
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _execute_run(
        self,
        state: RunState,
        runtime: RuntimeContext,
        events: EventSink,
    ) -> AgentLoopResult:
        events.emit("run.started", {"status": "running"})

        try:
            result = self._agent_runner(state.messages, runtime)
            if not isinstance(result, AgentLoopResult):
                raise TypeError("agent runner must return AgentLoopResult")
        except Exception as error:
            result = AgentLoopResult.failed(error)

        with state.lock:
            self._deny_pending_permissions(state, resolution="run_terminated")
            self._store().expire_pending_permissions(state.id)
            payload = {"status": result.status}
            if result.error:
                payload["error"] = result.error
            events.emit(f"run.{result.status}", payload)
        return result

    @staticmethod
    def _is_active(state: RunState) -> bool:
        with state.lock:
            return state.status in ACTIVE_STATUSES

    @staticmethod
    def _title_from_prompt(prompt: str, max_chars: int = 60) -> str:
        title = " ".join(prompt.split())
        if len(title) <= max_chars:
            return title
        return f"{title[:max_chars - 1]}…"

    @staticmethod
    def _deny_pending_permissions(
        state: RunState,
        *,
        resolution: PermissionResolution,
    ) -> None:
        for pending in state.permissions.values():
            if pending.decision is not None or pending.resolved.is_set():
                continue
            pending.decision = "deny"
            pending.resolution = resolution
            pending.resolved.set()
