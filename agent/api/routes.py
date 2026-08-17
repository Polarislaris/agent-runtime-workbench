"""REST and Server-Sent Event routes for the Agent Runtime MVP."""

from __future__ import annotations

import asyncio
import json
from queue import Empty, Queue
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from ..runtime.events import RunEvent
from ..database.autonomous_tasks import TASK_STORE
from ..database.worktree_reviews import WORKTREE_REVIEW_STORE
from ..database.worktrees import WORKTREE_STORE
from ..features.worktree_review import read_worktree_diff
from .models import CreateRunRequest, PermissionDecisionRequest, RunSnapshot
from .run_manager import (
    ActiveRunError,
    PermissionAlreadyResolvedError,
    PermissionNotFoundError,
    RunManager,
    RunNotFoundError,
    TERMINAL_STATUSES,
)
from .serializers import (
    sanitize,
    serialize_event,
    serialize_permission,
    serialize_run_snapshot,
)


router = APIRouter(prefix="/api", tags=["runs"])
SSE_HEARTBEAT_SECONDS = 15.0


def get_run_manager(request: Request) -> RunManager:
    """Read the lifespan-owned manager without creating hidden global state."""
    manager = getattr(request.app.state, "run_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="run manager is not initialized",
        )
    return manager


def _not_found(error: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


def _conflict(error: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


def _format_sse_data(event: dict) -> str:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event['id']}\nevent: {event['type']}\ndata: {data}\n\n"


def _format_sse(event: RunEvent) -> str:
    # SSE follows the same redaction rules as REST snapshot responses.  This
    # prevents a reconnecting browser from receiving a less-safe raw payload.
    return _format_sse_data(serialize_event(event))


def _format_heartbeat(run_id: str) -> str:
    data = json.dumps({"run_id": run_id}, separators=(",", ":"))
    return f"event: heartbeat\ndata: {data}\n\n"


def _next_event(
    event_queue: Queue[RunEvent],
    timeout_seconds: float,
) -> Optional[RunEvent]:
    try:
        return event_queue.get(timeout=timeout_seconds)
    except Empty:
        return None


def _resolve_sse_cursor(
    manager: RunManager,
    run_id: str,
    after: int,
    last_event_id: str | None,
) -> int:
    """Use the greatest valid query/header cursor for replay.

    Browsers automatically send the opaque SSE ``Last-Event-ID`` after an
    EventSource reconnect.  Manual clients often send a sequence number
    instead, so both forms are accepted.  An unknown/stale id is harmless: the
    explicit ``after`` cursor remains the durable fallback.
    """
    resolved = max(0, int(after))
    candidate = str(last_event_id or "").strip()
    if not candidate:
        return resolved
    try:
        header_sequence = int(candidate)
    except ValueError:
        header_sequence = manager.event_sequence(run_id, candidate) or 0
    return max(resolved, header_sequence) if header_sequence >= 0 else resolved


@router.post(
    "/runs",
    response_model=RunSnapshot,
    status_code=status.HTTP_201_CREATED,
)
def create_run(payload: CreateRunRequest, request: Request) -> RunSnapshot:
    manager = get_run_manager(request)
    try:
        return serialize_run_snapshot(manager.create_run(payload.prompt))
    except ActiveRunError as error:
        raise _conflict(error) from error


@router.get("/runs", response_model=list[RunSnapshot])
def list_runs(
    request: Request,
    response: Response,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[RunSnapshot]:
    manager = get_run_manager(request)
    try:
        snapshots = manager.list_runs(
            status=status_filter,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if len(snapshots) == limit:
        # The cursor is intentionally an opaque run id.  Clients need not know
        # the SQLite sort tuple that makes the next page deterministic.
        response.headers["X-Next-Cursor"] = snapshots[-1].id
    return [serialize_run_snapshot(snapshot) for snapshot in snapshots]


@router.get("/runs/{run_id}", response_model=RunSnapshot)
def get_run(run_id: str, request: Request) -> RunSnapshot:
    manager = get_run_manager(request)
    try:
        return serialize_run_snapshot(manager.snapshot(run_id))
    except RunNotFoundError as error:
        raise _not_found(error) from error


@router.post("/runs/{run_id}/cancel", response_model=RunSnapshot)
def cancel_run(run_id: str, request: Request) -> RunSnapshot:
    manager = get_run_manager(request)
    try:
        return serialize_run_snapshot(manager.cancel_run(run_id))
    except RunNotFoundError as error:
        raise _not_found(error) from error


@router.post(
    "/runs/{run_id}/permissions/{request_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def resolve_permission(
    run_id: str,
    request_id: str,
    payload: PermissionDecisionRequest,
    request: Request,
) -> Response:
    manager = get_run_manager(request)
    try:
        manager.resolve_permission(run_id, request_id, payload.decision)
    except (RunNotFoundError, PermissionNotFoundError) as error:
        raise _not_found(error) from error
    except PermissionAlreadyResolvedError as error:
        raise _conflict(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/runs/{run_id}/permissions")
def list_permission_history(run_id: str, request: Request) -> dict:
    """Return a durable approval audit trail for a single Web run."""
    manager = get_run_manager(request)
    try:
        permissions = manager.permission_history(run_id)
    except RunNotFoundError as error:
        raise _not_found(error) from error
    return {"items": [serialize_permission(item) for item in permissions]}


def _require_durable_run(manager: RunManager, run_id: str) -> None:
    """Keep project-level inspector APIs scoped to an existing selected run."""
    try:
        manager.snapshot(run_id)
    except RunNotFoundError as error:
        raise _not_found(error) from error


@router.get("/runs/{run_id}/tasks")
def list_run_tasks(run_id: str, request: Request) -> dict:
    """Expose the current workspace task board through the safe API boundary.

    s5 has no run-to-task relation yet; s6 will attach task ids to run events.
    The explicit scope prevents the UI from falsely claiming these are already
    filtered to one run.
    """
    _require_durable_run(get_run_manager(request), run_id)
    return {
        "scope": "workspace",
        "items": [sanitize(task.to_dict()) for task in TASK_STORE.list_tasks()],
    }


@router.get("/runs/{run_id}/agents")
def list_run_agents(run_id: str, request: Request) -> dict:
    """Expose durable teammate state; s6 will add run-specific correlation."""
    _require_durable_run(get_run_manager(request), run_id)
    return {
        "scope": "workspace",
        "items": [sanitize(agent.to_dict()) for agent in TASK_STORE.list_agents()],
    }


@router.get("/worktrees/{worktree_name}/diff")
def get_worktree_diff(worktree_name: str) -> dict:
    """Read a diff only when a user opens it, never from the run event table."""
    if WORKTREE_STORE.get_worktree(worktree_name) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="worktree not found")
    try:
        return sanitize(read_worktree_diff(worktree_name))
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/worktrees/{worktree_name}/checks")
def get_worktree_checks(worktree_name: str) -> dict:
    """Return persisted test/check records without rerunning commands."""
    if WORKTREE_STORE.get_worktree(worktree_name) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="worktree not found")
    return {
        "items": [
            sanitize(check.to_dict())
            for check in WORKTREE_REVIEW_STORE.list_checks(worktree_name)
        ]
    }


@router.get("/runs/{run_id}/events")
def stream_run_events(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: Optional[str] = Header(default=None),
) -> StreamingResponse:
    manager = get_run_manager(request)
    try:
        manager.snapshot(run_id)
    except RunNotFoundError as error:
        raise _not_found(error) from error
    state = manager.live_state_or_none(run_id)
    cursor = _resolve_sse_cursor(manager, run_id, after, last_event_id)

    # Register before the SQLite replay query.  DurableRunEventSink uses the
    # same per-run lock when publishing, so no event can fall between catch-up
    # and live delivery.  Historical runs have no live state to subscribe to.
    subscription_id: str | None = None
    event_queue: Queue[RunEvent] | None = None
    if state is not None:
        subscription_id, event_queue = manager.subscribe(run_id)

    async def generate_events():
        last_sequence = cursor
        try:
            # Catch up in bounded pages.  A long-lived run can exceed the store
            # page size, but the browser must still receive the complete range.
            while True:
                replay = manager.durable_events(
                    run_id,
                    after_sequence=last_sequence,
                )
                if not replay:
                    break
                for event in replay:
                    if event.sequence <= last_sequence:
                        continue
                    last_sequence = event.sequence
                    yield _format_sse(event)
                if len(replay) < 1_000:
                    break

            if event_queue is None:
                # Terminal historical runs are replay-only; leaving an SSE
                # socket open here would make a completed run look live.
                return

            while True:
                if await request.is_disconnected():
                    return

                current = manager.snapshot(state)
                if current.status in TERMINAL_STATUSES and event_queue.empty():
                    return

                event = await asyncio.to_thread(
                    _next_event,
                    event_queue,
                    SSE_HEARTBEAT_SECONDS,
                )
                if event is None:
                    yield _format_heartbeat(run_id)
                    continue
                if event.sequence <= last_sequence:
                    continue

                last_sequence = event.sequence
                yield _format_sse(event)
        finally:
            if subscription_id is not None:
                manager.unsubscribe(run_id, subscription_id)

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
