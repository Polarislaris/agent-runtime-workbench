"""S5 tests for safe, bounded browser serialization."""

from __future__ import annotations

from datetime import datetime, timezone

from agent.api.serializers import sanitize, serialize_event
from agent.runtime.events import RunEvent


def test_serializer_redacts_nested_secrets_and_embedded_shell_values():
    """Structured inputs and unstructured command output share one policy."""
    payload = sanitize({
        "headers": {"Authorization": "Bearer super-secret-value"},
        # Exceed the real configured 2,000-character preview limit so this
        # test verifies the production boundary rather than a test-only knob.
        "command_output": "SERVICE_TOKEN=not-for-the-browser\n" + "x" * 3_000,
        "path": "src/app.py",
    })

    assert payload["headers"]["Authorization"] == "***"
    assert "not-for-the-browser" not in payload["command_output"]
    assert "[truncated:" in payload["command_output"]
    assert payload["path"] == "src/app.py"


def test_serializer_keeps_unknown_event_types_as_safe_generic_events():
    """A newer runtime event must not make an older browser API fail closed."""
    event = RunEvent.create(
        run_id="run_001",
        sequence=1,
        event_type="future.runtime.event",
        payload={"token": "hidden", "summary": "still readable"},
        clock=lambda: datetime(2026, 8, 17, tzinfo=timezone.utc),
        event_id_factory=lambda: "evt_future",
    )

    serialized = serialize_event(event)

    assert serialized["type"] == "future.runtime.event"
    assert serialized["payload"] == {"token": "***", "summary": "still readable"}
