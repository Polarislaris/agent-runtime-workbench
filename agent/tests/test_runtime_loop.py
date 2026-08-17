"""Step 2/3 coverage for cancellation and observable Agent loop boundaries."""

from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.runtime import loop
from agent.runtime.event_payloads import summarize_tool_input, summarize_tool_output
from agent.runtime.events import RecordingEventSink, RuntimeContext


def text_response(text: str = "done") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
    )


def tool_response(*blocks: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(stop_reason="tool_use", content=list(blocks))


def tool_block(
    tool_use_id: str,
    name: str = "echo",
    **tool_input,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=tool_use_id,
        name=name,
        input=tool_input,
        type="tool_use",
    )


class FakeMessages:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = iter(responses)
        self.call_count = 0

    def create(self, **_kwargs) -> SimpleNamespace:
        self.call_count += 1
        return next(self.responses)


class RuntimeLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        replacements = {
            "assemble_tool_pool": lambda: ([], {}),
            "append_runtime_notifications": lambda _messages, _notifications, **_kwargs: False,
            "collect_runtime_notifications": lambda: [],
            "acknowledge_runtime_notifications": lambda _notifications: None,
            "consume_lead_inbox": lambda _messages, **_kwargs: None,
            "acknowledge_staged_inbox_messages": lambda: None,
            "tool_result_budget": lambda messages: messages,
            "snip_compact": lambda messages: messages,
            "micro_compact": lambda messages: messages,
            "load_memories": lambda messages: messages,
            "update_context": lambda **_kwargs: {},
            "get_system_prompt": lambda _context: "system",
            "estimate_token_count": lambda *_args, **_kwargs: 0,
            "serialize_message": repr,
            "serialized_size": lambda messages: sum(len(str(item)) for item in messages),
            "trigger_hooks": lambda *_args: None,
            "extract_memories": lambda _messages: None,
            "consolidate_memories": lambda: None,
            "should_run_background": lambda _name, _input: False,
        }
        patcher = patch.multiple(loop, **replacements)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install_model(self, responses: list[SimpleNamespace]) -> FakeMessages:
        messages_api = FakeMessages(responses)
        client_patcher = patch.object(
            loop,
            "get_client",
            return_value=SimpleNamespace(messages=messages_api),
        )
        client_patcher.start()
        self.addCleanup(client_patcher.stop)
        return messages_api

    def test_default_runtime_remains_backward_compatible(self) -> None:
        self.install_model([text_response()])

        result = loop.agent_loop([{"role": "user", "content": "hello"}])

        self.assertEqual(result.status, "completed")
        self.assertIsNone(result.error)

    def test_pre_cancelled_runtime_never_calls_model(self) -> None:
        runtime = RuntimeContext()
        runtime.cancellation.cancel()

        result = loop.agent_loop([], runtime=runtime)

        self.assertEqual(result.status, "cancelled")

    def test_cancellation_between_tools_skips_later_tool(self) -> None:
        first = tool_block("toolu_1", value="first")
        second = tool_block("toolu_2", value="second")
        self.install_model([tool_response(first, second)])
        runtime = RuntimeContext()
        calls: list[str] = []

        def echo(value: str) -> str:
            calls.append(value)
            runtime.cancellation.cancel()
            return f"echoed {value}"

        with patch.object(
            loop,
            "assemble_tool_pool",
            return_value=([{"name": "echo"}], {"echo": echo}),
        ):
            history = [{"role": "user", "content": "run both"}]
            result = loop.agent_loop(history, runtime=runtime)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(calls, ["first"])
        self.assertEqual(history[-1]["content"][0]["tool_use_id"], "toolu_1")

    def test_cancellation_during_permission_wait_skips_current_tool(self) -> None:
        block = tool_block("toolu_waiting", value="unsafe")
        self.install_model([tool_response(block)])
        runtime = RuntimeContext()
        calls: list[str] = []

        def cancel_during_pre_tool(*_args):
            runtime.cancellation.cancel()
            return None

        with patch.object(
            loop,
            "assemble_tool_pool",
            return_value=(
                [{"name": "echo"}],
                {"echo": lambda value: calls.append(value) or value},
            ),
        ), patch.object(
            loop,
            "trigger_hooks",
            side_effect=cancel_during_pre_tool,
        ):
            history = [{"role": "user", "content": "run"}]
            result = loop.agent_loop(history, runtime)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(calls, [])
        self.assertEqual(history[-1]["content"][0]["tool_use_id"], "toolu_waiting")
        self.assertTrue(history[-1]["content"][0]["is_error"])

    def test_model_and_assistant_events_are_ordered(self) -> None:
        self.install_model([text_response("observable answer")])
        sink = RecordingEventSink("run_events")

        result = loop.agent_loop(
            [{"role": "user", "content": "hello"}],
            runtime=RuntimeContext(run_id="run_events", events=sink),
        )

        events = sink.snapshot()
        self.assertEqual(result.status, "completed")
        self.assertEqual(
            [event.type for event in events],
            ["model.started", "model.completed", "assistant.message"],
        )
        self.assertEqual(events[-1].payload["text"], "observable answer")
        self.assertIn("duration_ms", events[1].payload)

    def test_tool_started_and_completed_share_tool_use_id(self) -> None:
        block = tool_block("toolu_shared", value="ok")
        self.install_model([tool_response(block), text_response()])
        sink = RecordingEventSink("run_tool")
        runtime = RuntimeContext(run_id="run_tool", events=sink)

        with patch.object(
            loop,
            "assemble_tool_pool",
            return_value=([{"name": "echo"}], {"echo": lambda value: f"echoed {value}"}),
        ):
            result = loop.agent_loop([{"role": "user", "content": "run"}], runtime)

        tool_events = [event for event in sink.snapshot() if event.type.startswith("tool.")]
        self.assertEqual(result.status, "completed")
        self.assertEqual([event.type for event in tool_events], ["tool.started", "tool.completed"])
        self.assertEqual(
            [event.payload["tool_use_id"] for event in tool_events],
            ["toolu_shared", "toolu_shared"],
        )
        self.assertEqual(tool_events[1].payload["output_preview"], "echoed ok")
        self.assertFalse(tool_events[0].payload["is_mock_mcp"])

    def test_handler_exception_emits_tool_failed(self) -> None:
        block = tool_block("toolu_failed", value="bad")
        self.install_model([tool_response(block), text_response()])
        sink = RecordingEventSink("run_failed_tool")

        def failing_handler(**_kwargs) -> str:
            raise RuntimeError("handler exploded")

        with patch.object(
            loop,
            "assemble_tool_pool",
            return_value=([{"name": "echo"}], {"echo": failing_handler}),
        ):
            result = loop.agent_loop(
                [{"role": "user", "content": "run"}],
                RuntimeContext(run_id="run_failed_tool", events=sink),
            )

        failed = [event for event in sink.snapshot() if event.type == "tool.failed"]
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].payload["tool_use_id"], "toolu_failed")
        self.assertIn("handler exploded", failed[0].payload["error"])

    def test_background_tool_completes_when_handler_finishes(self) -> None:
        block = tool_block("toolu_background", name="bash", command="pytest")
        self.install_model([tool_response(block), text_response()])
        sink = RecordingEventSink("run_background")

        def run_immediately(background_block, execute_tool, **_kwargs) -> str:
            execute_tool(background_block)
            return "bg_test"

        with patch.object(
            loop,
            "assemble_tool_pool",
            return_value=([{"name": "bash"}], {"bash": lambda command: "tests passed"}),
        ), patch.object(
            loop,
            "should_run_background",
            return_value=True,
        ), patch.object(
            loop,
            "start_background_task",
            side_effect=run_immediately,
        ):
            loop.agent_loop(
                [{"role": "user", "content": "test"}],
                RuntimeContext(run_id="run_background", events=sink),
            )

        tool_events = [event for event in sink.snapshot() if event.type.startswith("tool.")]
        self.assertEqual([event.type for event in tool_events], ["tool.started", "tool.completed"])
        self.assertTrue(tool_events[1].payload["background"])
        self.assertEqual(tool_events[1].payload["output_preview"], "tests passed")

    def test_mock_mcp_event_is_explicitly_labeled(self) -> None:
        block = tool_block("toolu_mcp", name="mcp__github__search", query="agent")
        self.install_model([tool_response(block), text_response()])
        sink = RecordingEventSink("run_mock_mcp")

        with patch.object(
            loop,
            "assemble_tool_pool",
            return_value=([{"name": block.name}], {block.name: lambda query: query}),
        ):
            loop.agent_loop(
                [{"role": "user", "content": "search"}],
                RuntimeContext(run_id="run_mock_mcp", events=sink),
            )

        started = next(event for event in sink.snapshot() if event.type == "tool.started")
        self.assertTrue(started.payload["is_mock_mcp"])

    def test_tool_summaries_redact_secrets_and_bound_output(self) -> None:
        summary = summarize_tool_input("deploy", {
            "path": "agent/runtime/loop.py",
            "environment": {"API_TOKEN": "do-not-leak"},
            "secret": "also-private",
        })
        output = summarize_tool_output("x" * 1_200)

        serialized = json.dumps(summary)
        self.assertNotIn("do-not-leak", serialized)
        self.assertNotIn("also-private", serialized)
        self.assertEqual(summary["environment"], "***")
        self.assertEqual(summary["secret"], "***")
        self.assertLessEqual(len(output), 1_001)


if __name__ == "__main__":
    unittest.main()
