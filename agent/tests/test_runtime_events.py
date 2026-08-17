"""Tests for the framework-neutral Step 1 runtime event boundary."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest

from agent.runtime.events import (
    CancellationToken,
    NullEventSink,
    RecordingEventSink,
    RunEvent,
    RUNTIME_EVENT_SCHEMA_VERSION,
    RuntimeContext,
)


FIXED_TIME = datetime(2026, 8, 16, 6, 30, 0, 123000, tzinfo=timezone.utc)


class RuntimeEventTests(unittest.TestCase):
    def make_sink(self) -> RecordingEventSink:
        event_ids = iter(["evt_001", "evt_002", "evt_003"])
        return RecordingEventSink(
            "run_test",
            clock=lambda: FIXED_TIME,
            event_id_factory=lambda: next(event_ids),
        )

    def test_recording_sink_allocates_strictly_increasing_sequences(self) -> None:
        sink = self.make_sink()

        sink.emit("run.started", {"status": "running"})
        sink.emit("tool.started", {"tool": "read_file"})

        events = sink.snapshot()
        self.assertEqual([event.sequence for event in events], [1, 2])
        self.assertEqual([event.id for event in events], ["evt_001", "evt_002"])
        self.assertEqual(sink.last_sequence, 2)

    def test_event_is_json_serializable_and_uses_utc_timestamp(self) -> None:
        event = RunEvent.create(
            run_id="run_test",
            sequence=1,
            event_type="assistant.message",
            payload={"text": "完成"},
            clock=lambda: FIXED_TIME,
            event_id_factory=lambda: "evt_fixed",
        )

        data = event.to_dict()
        self.assertEqual(data["schema_version"], RUNTIME_EVENT_SCHEMA_VERSION)
        self.assertEqual(data["created_at"], "2026-08-16T06:30:00.123000Z")
        self.assertEqual(json.loads(json.dumps(data, ensure_ascii=False)), data)

    def test_sink_detaches_payload_from_caller_mutation(self) -> None:
        sink = self.make_sink()
        payload = {"tool": "bash", "input": {"command": "pytest"}}

        sink.emit("tool.started", payload)
        payload["input"]["command"] = "changed"

        self.assertEqual(
            sink.snapshot()[0].payload["input"]["command"],
            "pytest",
        )

    def test_event_rejects_non_json_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON serializable"):
            RunEvent.create(
                run_id="run_test",
                sequence=1,
                event_type="invalid",
                payload={"value": object()},
            )

    def test_event_validates_identity_sequence_and_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "run_id"):
            RunEvent.create(
                run_id=" ",
                sequence=1,
                event_type="run.started",
                payload={},
            )
        with self.assertRaisesRegex(ValueError, "sequence"):
            RunEvent.create(
                run_id="run_test",
                sequence=0,
                event_type="run.started",
                payload={},
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            RunEvent.create(
                run_id="run_test",
                sequence=1,
                event_type="run.started",
                payload={},
                clock=lambda: datetime(2026, 8, 16),
            )

    def test_cancellation_token_changes_once_cancelled(self) -> None:
        token = CancellationToken()

        self.assertFalse(token.is_cancelled())
        self.assertFalse(token.wait(timeout=0))
        token.cancel()
        self.assertTrue(token.is_cancelled())
        self.assertTrue(token.wait(timeout=0))

    def test_runtime_contexts_do_not_share_cancellation_tokens(self) -> None:
        first = RuntimeContext()
        second = RuntimeContext()

        first.cancellation.cancel()

        self.assertTrue(first.cancellation.is_cancelled())
        self.assertFalse(second.cancellation.is_cancelled())

    def test_null_sink_has_no_side_effect(self) -> None:
        sink = NullEventSink()
        self.assertIsNone(sink.emit("ignored", {"value": 1}))


if __name__ == "__main__":
    unittest.main()
