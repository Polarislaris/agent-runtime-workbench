"""Lightweight cross-feature event context for observable Agent domains.

Feature modules such as tasks, worktrees, and teammates should not import the
Web RunManager or receive the mutable lead ``messages`` list.  This module
passes only the immutable run identity and the existing EventSink.  A
``ContextVar`` covers synchronous tool handlers; explicit context capture is
used when work moves to a Python thread.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from .events import EventSink, RuntimeContext


@dataclass(frozen=True)
class DomainEventContext:
    """The minimum immutable data a feature needs to publish a run event."""

    run_id: str
    events: EventSink


_CURRENT_CONTEXT: ContextVar[Optional[DomainEventContext]] = ContextVar(
    "agent_domain_event_context",
    default=None,
)


def current_domain_event_context() -> Optional[DomainEventContext]:
    """Return the context bound to this thread, if it belongs to a Web run."""
    return _CURRENT_CONTEXT.get()


@contextmanager
def activate_domain_events(
    context: Optional[DomainEventContext],
) -> Iterator[None]:
    """Temporarily bind a captured context in a worker thread or tool call."""
    token: Token[Optional[DomainEventContext]] = _CURRENT_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_CONTEXT.reset(token)


@contextmanager
def activate_runtime_domain_events(runtime: RuntimeContext) -> Iterator[None]:
    """Bind the domain context for one lead ``agent_loop`` invocation."""
    context = DomainEventContext(run_id=runtime.run_id, events=runtime.events)
    with activate_domain_events(context):
        yield


def emit_domain_event(event_type: str, payload: dict[str, Any]) -> bool:
    """Publish a safe domain event only while a run context is active.

    CLI calls intentionally have a NullEventSink, so this keeps all existing
    feature functions usable outside FastAPI without adding Web-only branches.
    ``parent_run_id`` lets a later Inspector relate events emitted from a
    subagent/teammate/background thread back to its lead run.
    """
    context = current_domain_event_context()
    if context is None:
        return False
    event_payload = dict(payload)
    event_payload.setdefault("parent_run_id", context.run_id)
    context.events.emit(event_type, event_payload)
    return True


def emit_captured_domain_event(
    context: Optional[DomainEventContext],
    event_type: str,
    payload: dict[str, Any],
) -> bool:
    """Publish from a worker that received an explicit captured context."""
    if context is None:
        return False
    with activate_domain_events(context):
        return emit_domain_event(event_type, payload)
