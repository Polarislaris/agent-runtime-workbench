"""Safe, bounded JSON views for the browser-facing Agent Runtime API.

The run database deliberately retains the original structured messages and
event payloads for audit/replay.  HTTP and SSE must not expose those raw values
directly: a tool result can contain an environment variable, an authorization
header, or simply far more terminal output than a browser should render.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..config import RUN_EVENT_PREVIEW_CHARS
from ..database.runs import StoredPermissionRequest
from ..runtime.events import RunEvent
from .models import RunSnapshot


REDACTED = "***"
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?key|secret|token|password|passwd|"
    r"authorization|cookie|set-cookie|private[_-]?key)",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_ENV_ASSIGNMENT = re.compile(
    r"(?im)\b([A-Z][A-Z0-9_]*(?:TOKEN|SECRET|API[_-]?KEY|PASSWORD|PASSWD)"
    r"[A-Z0-9_]*)\s*=\s*[^\s'\"]+"
)


def _truncate(text: str, *, limit: int = RUN_EVENT_PREVIEW_CHARS) -> str:
    """Keep UI previews bounded without modifying durable source records."""
    bounded_limit = max(200, int(limit))
    if len(text) <= bounded_limit:
        return text
    omitted = len(text) - bounded_limit
    return f"{text[:bounded_limit]}\n\n[truncated: {omitted} chars omitted]"


def _redact_text(value: str) -> str:
    """Mask common embedded credentials before applying the output limit."""
    redacted = _BEARER_VALUE.sub("Bearer ***", value)
    redacted = _ENV_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=***", redacted)
    return _truncate(redacted)


def sanitize(value: Any, *, field_name: str = "") -> Any:
    """Recursively produce a JSON-safe, redacted, size-bounded API value.

    Key-based filtering protects structured tool input; text filtering covers
    shell output and copied ``.env`` lines where no structured key survives.
    Unknown objects are converted to bounded strings rather than making an API
    response fail after a successful Agent run.
    """
    if _SENSITIVE_KEY.search(field_name):
        return REDACTED
    if isinstance(value, str):
        return _redact_text(value)
    # This project supports Python 3.9, where the ``bool | int`` syntax is not
    # available at runtime even when postponed annotations are enabled.
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): sanitize(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return _redact_text(str(value))


def serialize_event(event: RunEvent | Mapping[str, Any]) -> dict[str, Any]:
    """Return one SSE/REST event using the same security policy everywhere."""
    source = event.to_dict() if isinstance(event, RunEvent) else dict(event)
    return {
        "id": str(source.get("id", "")),
        "run_id": str(source.get("run_id", "")),
        "sequence": int(source.get("sequence", 0)),
        "schema_version": int(source.get("schema_version", 1)),
        "type": str(source.get("type", "unknown")),
        "created_at": str(source.get("created_at", "")),
        "payload": sanitize(source.get("payload", {})),
    }


def serialize_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Return a persisted conversation message without leaking raw tool data."""
    return {
        "role": str(message.get("role", "")),
        "content": sanitize(message.get("content")),
    }


def serialize_permission(request: StoredPermissionRequest) -> dict[str, Any]:
    """Expose an approval audit row through the same redaction boundary."""
    return {
        "id": request.id,
        "run_id": request.run_id,
        "tool_name": request.tool_name,
        "input_preview": sanitize(request.input_preview),
        "reason": _redact_text(request.reason),
        "status": request.status,
        "decision": request.decision,
        "created_at": request.created_at,
        "resolved_at": request.resolved_at,
    }


def serialize_run_snapshot(snapshot: RunSnapshot) -> dict[str, Any]:
    """Detach a RunSnapshot from raw storage values before returning JSON."""
    return {
        "id": snapshot.id,
        "title": _redact_text(snapshot.title),
        "status": snapshot.status,
        "messages": [serialize_message(message) for message in snapshot.messages],
        "events": [serialize_event(event) for event in snapshot.events],
        "started_at": snapshot.started_at,
        "completed_at": snapshot.completed_at,
        "error": _redact_text(snapshot.error) if snapshot.error else None,
        "last_sequence": snapshot.last_sequence,
    }
