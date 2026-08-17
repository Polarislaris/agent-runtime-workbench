"""Framework-neutral runtime primitives for observable Agent runs.

This is the Step 1 boundary described in ``frontend/DESIGN.md``.  It contains
no FastAPI, SSE, React, or database code.  Later MVP steps can provide an
``EventSink`` that publishes these events without coupling the Agent loop to a
particular delivery mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from threading import Event, Lock
from typing import Any, Callable, Literal, Protocol
from uuid import uuid4


JsonObject = dict[str, Any]
PermissionDecision = Literal["allow", "deny"]
AgentLoopStatus = Literal["completed", "cancelled", "failed"]
Clock = Callable[[], datetime]
EventIdFactory = Callable[[], str]

# The version belongs to the event envelope rather than individual event
# payloads. Consumers can therefore handle a future event type generically
# instead of failing when its payload evolves.
RUNTIME_EVENT_SCHEMA_VERSION = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_event_id() -> str:
    return f"evt_{uuid4().hex}"


def _isoformat_utc(value: datetime) -> str:
    """Return an RFC 3339-style UTC timestamp ending in ``Z``."""
    if value.tzinfo is None:
        raise ValueError("runtime event timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _copy_json_object(payload: JsonObject) -> JsonObject:
    """Validate and detach event payloads from mutable caller-owned objects."""
    try:
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        result = json.loads(serialized)
    except (TypeError, ValueError) as error:
        raise ValueError(f"runtime event payload must be JSON serializable: {error}") from error
    if not isinstance(result, dict):  # Defensive: JsonObject should stay an object.
        raise ValueError("runtime event payload must be a JSON object")
    return result


@dataclass(frozen=True)
class RunEvent:
    """One immutable, JSON-safe event emitted by an Agent run."""

    id: str
    run_id: str
    sequence: int
    schema_version: int
    type: str
    created_at: str
    payload: JsonObject

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        sequence: int,
        event_type: str,
        payload: JsonObject,
        clock: Clock = _utc_now,
        event_id_factory: EventIdFactory = _new_event_id,
    ) -> "RunEvent":
        """Create a validated event with an injected clock/id factory for tests."""
        normalized_run_id = str(run_id).strip()
        normalized_type = str(event_type).strip()
        if not normalized_run_id:
            raise ValueError("run_id must not be empty")
        if sequence < 1:
            raise ValueError("runtime event sequence must be at least 1")
        if not normalized_type:
            raise ValueError("runtime event type must not be empty")

        event_id = str(event_id_factory()).strip()
        if not event_id:
            raise ValueError("runtime event id must not be empty")

        return cls(
            id=event_id,
            run_id=normalized_run_id,
            sequence=sequence,
            schema_version=RUNTIME_EVENT_SCHEMA_VERSION,
            type=normalized_type,
            created_at=_isoformat_utc(clock()),
            payload=_copy_json_object(payload),
        )

    def to_dict(self) -> JsonObject:
        """Return a detached dictionary suitable for JSON/API serialization."""
        return {
            "id": self.id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "schema_version": self.schema_version,
            "type": self.type,
            "created_at": self.created_at,
            "payload": _copy_json_object(self.payload),
        }


class EventSink(Protocol):
    """Destination for framework-neutral runtime events."""

    def emit(self, event_type: str, payload: JsonObject) -> None:
        """Publish one event payload for the sink's run."""
        ...


class NullEventSink:
    """Default sink that preserves existing CLI behavior without side effects."""

    def emit(self, event_type: str, payload: JsonObject) -> None:
        del event_type, payload


class MessageJournal(Protocol):
    """Durable observer for messages that enter one Agent conversation."""

    def append(self, message: Any) -> None:
        """Record one message after it has entered in-memory history."""
        ...


class NullMessageJournal:
    """Default journal that keeps CLI calls free of database side effects."""

    def append(self, message: Any) -> None:
        del message


class RecordingEventSink:
    """Thread-safe in-memory sink for tests and later RunManager integration."""

    def __init__(
        self,
        run_id: str,
        *,
        initial_sequence: int = 0,
        clock: Clock = _utc_now,
        event_id_factory: EventIdFactory = _new_event_id,
    ) -> None:
        if not str(run_id).strip():
            raise ValueError("run_id must not be empty")
        if initial_sequence < 0:
            raise ValueError("initial_sequence must not be negative")
        self.run_id = str(run_id).strip()
        self._sequence = initial_sequence
        self._clock = clock
        self._event_id_factory = event_id_factory
        self._events: list[RunEvent] = []
        self._lock = Lock()

    def emit(self, event_type: str, payload: JsonObject) -> None:
        """Allocate sequence and append atomically, preserving event order."""
        with self._lock:
            sequence = self._sequence + 1
            event = RunEvent.create(
                run_id=self.run_id,
                sequence=sequence,
                event_type=event_type,
                payload=payload,
                clock=self._clock,
                event_id_factory=self._event_id_factory,
            )
            self._events.append(event)
            self._sequence = sequence

    def snapshot(self) -> list[RunEvent]:
        """Return a stable copy of all events recorded so far."""
        with self._lock:
            return list(self._events)

    @property
    def last_sequence(self) -> int:
        """Return the most recently allocated sequence number."""
        with self._lock:
            return self._sequence


class CancellationToken:
    """Thread-safe cooperative cancellation signal for a future Web run."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until cancelled, returning false when the timeout expires."""
        return self._event.wait(timeout)


@dataclass(frozen=True)
class AgentLoopResult:
    """Explicit terminal state returned by one Agent loop invocation."""

    status: AgentLoopStatus
    error: str | None = None

    @classmethod
    def completed(cls) -> "AgentLoopResult":
        return cls(status="completed")

    @classmethod
    def cancelled(cls) -> "AgentLoopResult":
        return cls(status="cancelled")

    @classmethod
    def failed(cls, error: object) -> "AgentLoopResult":
        return cls(status="failed", error=str(error))


class PermissionProvider(Protocol):
    """Decision boundary implemented by CLI and Web providers in later steps."""

    def decide(
        self,
        tool_name: str,
        args: JsonObject,
        reason: str,
    ) -> PermissionDecision:
        ...


@dataclass
class RuntimeContext:
    """Dependencies and identity shared by one Agent loop invocation."""

    run_id: str = "cli"
    events: EventSink = field(default_factory=NullEventSink)
    message_journal: MessageJournal = field(default_factory=NullMessageJournal)
    permissions: PermissionProvider | None = None
    cancellation: CancellationToken = field(default_factory=CancellationToken)
