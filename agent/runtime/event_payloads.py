"""Small, JSON-safe payload helpers for observable Agent runtime events."""

from __future__ import annotations

import json
import time
from typing import Any, Callable


INPUT_STRING_PREVIEW_CHARS = 500
OUTPUT_PREVIEW_CHARS = 1_000
REDACTED = "***"
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
    "api_key",
    "apikey",
)


def elapsed_ms(started_at: float, *, now: Callable[[], float] = time.monotonic) -> int:
    """Return a stable, non-negative elapsed duration for an event payload."""
    return max(0, round((now() - started_at) * 1_000))


def extract_assistant_text(content: Any) -> str:
    """Join text blocks from either Anthropic SDK objects or plain dictionaries."""
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, dict):
            block_type = block.get("type")
            text = block.get("text")
        else:
            block_type = getattr(block, "type", None)
            text = getattr(block, "text", None)
        if block_type == "text" and isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    if normalized in {"env", "environment", "key"}:
        return True
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _safe_value(value: Any, *, string_limit: int = INPUT_STRING_PREVIEW_CHARS) -> Any:
    """Detach arbitrary tool data while redacting secrets and bounding strings."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= string_limit:
            return value
        return f"{value[:string_limit]}…"
    if isinstance(value, dict):
        return {
            str(key): REDACTED if _is_sensitive_key(key) else _safe_value(item, string_limit=string_limit)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, string_limit=string_limit) for item in value]
    return _safe_value(str(value), string_limit=string_limit)


def summarize_tool_input(tool_name: str, args: Any) -> dict[str, Any]:
    """Return a bounded, secret-redacted description of a tool invocation."""
    del tool_name  # Reserved for future per-tool summaries such as read/bash.
    safe_args = _safe_value(args)
    if not isinstance(safe_args, dict):
        safe_args = {"value": safe_args}
    return safe_args


def summarize_tool_output(output: Any) -> str:
    """Return only a short display preview; full tool output remains in history."""
    if isinstance(output, str):
        rendered = output
    else:
        try:
            rendered = json.dumps(_safe_value(output), ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = str(output)
    if len(rendered) > OUTPUT_PREVIEW_CHARS:
        rendered = f"{rendered[:OUTPUT_PREVIEW_CHARS]}…"
    return rendered
