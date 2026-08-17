"""Main agent loop orchestration."""

from __future__ import annotations

import time

from ..features.background_tasks import should_run_background, start_background_task
from .client import get_client
from .compact import (
    compact_history,
    is_prompt_too_long_error,
    micro_compact,
    reactive_compact,
    snip_compact,
    tool_result_budget,
)
from ..config import COMPACT_TOKEN_THRESHOLD
from .error_recovery import (
    CONTINUATION_PROMPT,
    RecoveryState,
    can_continue_after_truncation,
    escalate_output_tokens,
    record_continuation_request,
    should_escalate_output_tokens,
    with_retry,
)
from ..tooling.hooks import trigger_hooks
from ..features.memory import consolidate_memories, extract_memories, load_memories
from .messages import estimate_token_count, serialize_message, serialized_size
from .notifications import (
    acknowledge_runtime_notifications,
    append_runtime_notifications,
    collect_runtime_notifications,
    notification_content,
)
from ..prompts import get_system_prompt, update_context
from ..features.team import acknowledge_staged_inbox_messages
from ..database.team_protocols import consume_lead_inbox
from ..tooling.pool import assemble_tool_pool
from .event_payloads import (
    elapsed_ms,
    extract_assistant_text,
    summarize_tool_input,
    summarize_tool_output,
)
from .events import AgentLoopResult, RuntimeContext
from .domain_events import (
    activate_runtime_domain_events,
    current_domain_event_context,
    emit_captured_domain_event,
    emit_domain_event,
)


def execute_tool_call(block, handlers) -> str:
    """Execute one already-authorized tool call and return its text output."""
    handler = handlers.get(block.name)
    if not handler:
        return f"Error: Unknown tool: {block.name}"

    try:
        return handler(**block.input)
    except TypeError as e:
        return f"Error: Invalid tool input for {block.name}: {e}"
    except Exception as e:
        return f"Error: Tool {block.name} failed: {e}"


def extract_tool_use_blocks(content) -> list:
    """Return actual tool-use blocks, independent of a response stop reason."""
    return [
        block
        for block in content or []
        if getattr(block, "type", None) == "tool_use"
    ]


def _emit_assistant_message(runtime: RuntimeContext, content) -> None:
    text = extract_assistant_text(content)
    if text:
        runtime.events.emit("assistant.message", {"text": text})


def _append_history_message(messages: list, message: dict, runtime: RuntimeContext) -> None:
    """Append to live history before journaling the same message for replay.

    The in-memory append must happen first because the Agent continues from that
    list immediately. A durable journal failure then fails the Web run instead
    of letting live and replay histories silently diverge.
    """
    messages.append(message)
    runtime.message_journal.append(message)


def _tool_event_payload(block) -> dict:
    return {
        "tool_use_id": block.id,
        "tool": block.name,
        "input_summary": summarize_tool_input(block.name, block.input),
        # s19 MCP tools are intentionally mock/in-process teaching handlers.
        "is_mock_mcp": block.name.startswith("mcp__"),
    }


def _append_tool_round_results(
    messages: list,
    results: list,
    runtime: RuntimeContext,
) -> None:
    """Persist completed results before continuing or returning on cancellation."""
    completed_notifications = collect_runtime_notifications()
    user_content = notification_content(completed_notifications)
    user_content.extend(results)
    if user_content:
        _append_history_message(
            messages,
            {"role": "user", "content": user_content},
            runtime,
        )
        for notification in completed_notifications:
            if notification.source == "cron":
                emit_domain_event("cron.fired", dict(notification.metadata))
    acknowledge_runtime_notifications(completed_notifications)
    acknowledge_staged_inbox_messages()


def agent_loop(
    messages: list,
    runtime: RuntimeContext | None = None,
) -> AgentLoopResult:
    """Call the model, execute requested tools, and repeat until the model stops."""
    runtime = runtime or RuntimeContext()
    # Tool handlers can now emit domain events without receiving mutable
    # conversation history. Background/teammate workers explicitly capture
    # this same lightweight context before crossing a thread boundary.
    with activate_runtime_domain_events(runtime):
        return _agent_loop_with_runtime(messages, runtime)


def _agent_loop_with_runtime(
    messages: list,
    runtime: RuntimeContext,
) -> AgentLoopResult:
    """Implementation kept separate so the public wrapper owns context scope."""
    rounds_since_todo = 0
    recovery = RecoveryState()

    # Memory extraction should see the richest available context, not only a
    # lossy compact summary. Keep the largest serialized snapshot seen this turn.
    memory_extraction_snapshot = [serialize_message(message) for message in messages]

    while True:
        if runtime.cancellation.is_cancelled():
            return AgentLoopResult.cancelled()

        # s19: this rebuild is intentional. A successful ``connect_mcp`` from
        # the previous tool round changes the next model request's available
        # tools. The MCP implementation is mock/in-process for this lesson.
        tools, handlers = assemble_tool_pool()

        if rounds_since_todo >= 3 and messages:
            print("\033[90m[TODO] Reminder injected: Update your todos.\033[0m")
            _append_history_message(
                messages,
                {"role": "user", "content": "<reminder>Update your todos.</reminder>"},
                runtime,
            )
            rounds_since_todo = 0

        # Runtime events are always injected by the loop, before the next model
        # call.  Cron only wakes this loop; background completions are acked
        # after their notification has entered durable message history.
        runtime_notifications = collect_runtime_notifications()
        appended_notifications = append_runtime_notifications(
            messages,
            runtime_notifications,
            on_appended=runtime.message_journal.append,
        )
        if appended_notifications:
            for notification in runtime_notifications:
                if notification.source == "cron":
                    emit_domain_event("cron.fired", dict(notification.metadata))

        # s16: surface teammate messages before the next LLM call. Protocol
        # routing happens before history append; ack happens after append.
        consume_lead_inbox(
            messages,
            on_appended=runtime.message_journal.append,
        )

        pre_compact_snapshot = [serialize_message(message) for message in messages]
        if serialized_size(pre_compact_snapshot) >= serialized_size(memory_extraction_snapshot):
            memory_extraction_snapshot = pre_compact_snapshot

        # Cheap context controls run before any model call.
        messages[:] = tool_result_budget(messages)
        messages[:] = snip_compact(messages)
        messages[:] = micro_compact(messages)

        # s10: Build SYSTEM from runtime context. The context is based on real
        # state such as registered tools and MEMORY.md, not keywords in messages.
        context = update_context(
            messages=messages,
            enabled_tools=[tool["name"] for tool in tools],
        )
        system_prompt = get_system_prompt(context)

        # Memory index lives in SYSTEM; selected full memories are temporary turn
        # context so they do not pollute history or transcripts.
        call_messages = load_memories(messages)

        token_estimate = estimate_token_count(
            call_messages,
            system_prompt=system_prompt,
            tools=tools,
        )
        if token_estimate > COMPACT_TOKEN_THRESHOLD:
            print(f"\033[90m[auto compact] estimated {token_estimate} tokens\033[0m")
            memory_extraction_snapshot = pre_compact_snapshot
            messages[:] = compact_history(messages)
            emit_domain_event("context.compacted", {
                "reason": "token_threshold",
                "token_estimate": token_estimate,
            })
            context = update_context(
                messages=messages,
                enabled_tools=[tool["name"] for tool in tools],
            )
            system_prompt = get_system_prompt(context)
            call_messages = load_memories(messages)

        if runtime.cancellation.is_cancelled():
            return AgentLoopResult.cancelled()

        try:
            # s11: with_retry owns transient 429/529 retry policy. The loop
            # supplies the actual request because it owns system/messages/tools.
            def request_model(model):
                started_at = time.monotonic()
                runtime.events.emit("model.started", {
                    "model": model,
                    "message_count": len(call_messages),
                })
                model_response = get_client().messages.create(
                    model=model,
                    system=system_prompt,
                    messages=call_messages,
                    tools=tools,
                    max_tokens=recovery.max_tokens,
                )
                runtime.events.emit("model.completed", {
                    "model": model,
                    "duration_ms": elapsed_ms(started_at),
                    "stop_reason": getattr(model_response, "stop_reason", None),
                })
                return model_response

            response = with_retry(
                request_model,
                recovery,
                on_retry=lambda payload: emit_domain_event("retry.scheduled", payload),
            )
        except Exception as e:
            if is_prompt_too_long_error(e) and not recovery.has_attempted_reactive_compact:
                # s11: prompt_too_long gets one aggressive compact attempt, then
                # retries from the top of the loop with a smaller history.
                memory_extraction_snapshot = pre_compact_snapshot
                messages[:] = reactive_compact(messages)
                emit_domain_event("context.compacted", {
                    "reason": "prompt_too_long",
                })
                recovery.has_attempted_reactive_compact = True
                continue
            print(f"\033[31m[recovery] unrecoverable LLM error: {e}\033[0m")
            return AgentLoopResult.failed(e)

        if runtime.cancellation.is_cancelled():
            return AgentLoopResult.cancelled()

        tool_blocks = extract_tool_use_blocks(response.content)

        # s11: Handle output truncation before appending assistant content.
        # The first max_tokens stop retries the exact same request with a larger
        # output cap, so the truncated answer does not enter history. Actual
        # tool-use blocks take precedence over this advisory stop reason.
        if response.stop_reason == "max_tokens" and not tool_blocks:
            if should_escalate_output_tokens(recovery):
                escalate_output_tokens(recovery)
                continue

            _append_history_message(
                messages,
                {"role": "assistant", "content": response.content},
                runtime,
            )
            _emit_assistant_message(runtime, response.content)
            if can_continue_after_truncation(recovery):
                record_continuation_request(recovery)
                _append_history_message(
                    messages,
                    {"role": "user", "content": CONTINUATION_PROMPT},
                    runtime,
                )
                continue

            print("\033[90m[max_tokens] stopped after continuation limit\033[0m")
            return AgentLoopResult.failed("max_tokens continuation limit reached")

        _append_history_message(
            messages,
            {"role": "assistant", "content": response.content},
            runtime,
        )
        _emit_assistant_message(runtime, response.content)

        if not tool_blocks:
            force = trigger_hooks("Stop", messages)
            if force:
                _append_history_message(
                    messages,
                    {"role": "user", "content": force},
                    runtime,
                )
                continue

            # The turn is complete: extract durable memories and occasionally
            # consolidate the memory store.
            extract_memories(memory_extraction_snapshot)
            consolidate_memories()
            return AgentLoopResult.completed()

        results = []
        used_todo_write = False
        compact_requested = False

        for block in tool_blocks:
            if runtime.cancellation.is_cancelled():
                _append_tool_round_results(messages, results, runtime)
                return AgentLoopResult.cancelled()

            blocked = trigger_hooks("PreToolUse", block, runtime)
            if blocked:
                print(f"\033[31mPermission denied: {block.name} {block.input}\033[0m")
                failed_payload = _tool_event_payload(block)
                failed_payload["error"] = str(blocked)
                runtime.events.emit("tool.failed", failed_payload)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(blocked),
                    "is_error": True,
                })
                continue

            # Permission providers may block while waiting for a user. Cancel
            # can arrive during that wait, so check again before the handler.
            if runtime.cancellation.is_cancelled():
                cancelled_output = "Cancelled before tool execution"
                failed_payload = _tool_event_payload(block)
                failed_payload["error"] = cancelled_output
                runtime.events.emit("tool.failed", failed_payload)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": cancelled_output,
                    "is_error": True,
                })
                _append_tool_round_results(messages, results, runtime)
                return AgentLoopResult.cancelled()

            tool_started_at = time.monotonic()
            base_payload = _tool_event_payload(block)
            runtime.events.emit("tool.started", base_payload)

            if block.name == "compact":
                memory_extraction_snapshot = [serialize_message(message) for message in messages]
                messages[:] = compact_history(messages, label="Compacted by compact tool")
                emit_domain_event("context.compacted", {
                    "reason": "compact_tool",
                    "tool_use_id": block.id,
                })
                _append_history_message(
                    messages,
                    {"role": "assistant", "content": [block]},
                    runtime,
                )
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "[Compacted. History summarized.]",
                })
                completed_payload = dict(base_payload)
                completed_payload.update({
                    "duration_ms": elapsed_ms(tool_started_at),
                    "output_preview": summarize_tool_output(
                        "[Compacted. History summarized.]"
                    ),
                })
                runtime.events.emit("tool.completed", completed_payload)
                compact_requested = True
                break

            if should_run_background(block.name, block.input):
                try:
                    background_event_context = current_domain_event_context()

                    def execute_background_tool(
                        background_block,
                        event_payload=dict(base_payload),
                        started_at=tool_started_at,
                        background_handlers=handlers,
                    ) -> str:
                        background_output = execute_tool_call(
                            background_block,
                            background_handlers,
                        )
                        try:
                            trigger_hooks("PostToolUse", background_block, background_output)
                        except Exception as e:
                            background_output = (
                                f"Error: PostToolUse hook failed for "
                                f"{background_block.name}: {e}"
                            )

                        terminal_payload = dict(event_payload)
                        terminal_payload.update({
                            "duration_ms": elapsed_ms(started_at),
                            "background": True,
                            "output_preview": summarize_tool_output(background_output),
                        })
                        if background_output.startswith("Error:"):
                            terminal_payload["error"] = background_output
                            runtime.events.emit("tool.failed", terminal_payload)
                        else:
                            runtime.events.emit("tool.completed", terminal_payload)
                        return background_output

                    bg_id = start_background_task(
                        block,
                        execute_background_tool,
                        on_started=lambda task: emit_domain_event("background.started", {
                            "background_id": task.id,
                            "tool_use_id": task.tool_use_id,
                            "tool": task.tool_name,
                        }),
                        on_completed=lambda task: emit_captured_domain_event(
                            background_event_context,
                            "background.completed",
                            {
                                "background_id": task.id,
                                "tool_use_id": task.tool_use_id,
                                "tool": task.tool_name,
                                "status": task.status,
                                "duration_ms": max(
                                    0,
                                    round(((task.completed_at or task.started_at) - task.started_at) * 1_000),
                                ),
                            },
                        ),
                    )
                    output = (
                        f"[Background task {bg_id} started] "
                        "Result will be injected as <task_notification> when complete."
                    )
                except RuntimeError as e:
                    output = f"Error: {e}"

                print(f"\033[33m$ {block.name} {block.input}\033[0m")
                print(output[:200])
                result = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
                if output.startswith("Error:"):
                    result["is_error"] = True
                results.append(result)
                if output.startswith("Error:"):
                    terminal_payload = dict(base_payload)
                    terminal_payload.update({
                        "duration_ms": elapsed_ms(tool_started_at),
                        "background": True,
                        "output_preview": summarize_tool_output(output),
                    })
                    terminal_payload["error"] = output
                    runtime.events.emit("tool.failed", terminal_payload)
                continue

            print(f"\033[33m$ {block.name} {block.input}\033[0m")
            output = execute_tool_call(block, handlers)

            if block.name == "todo_write" and not output.startswith("Error:"):
                used_todo_write = True

            try:
                trigger_hooks("PostToolUse", block, output)
            except Exception as e:
                output = f"Error: PostToolUse hook failed for {block.name}: {e}"

            print(output[:200])
            result = {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            }
            if output.startswith("Error:"):
                result["is_error"] = True
            results.append(result)
            terminal_payload = dict(base_payload)
            terminal_payload.update({
                "duration_ms": elapsed_ms(tool_started_at),
                "output_preview": summarize_tool_output(output),
            })
            if output.startswith("Error:"):
                terminal_payload["error"] = output
                runtime.events.emit("tool.failed", terminal_payload)
            else:
                runtime.events.emit("tool.completed", terminal_payload)

        # Runtime events are independent of the original tool_use_id. Collect
        # them through the same entry point and acknowledge only after the
        # combined notification/tool-result message is appended.
        _append_tool_round_results(messages, results, runtime)
        if runtime.cancellation.is_cancelled():
            return AgentLoopResult.cancelled()
        rounds_since_todo = 0 if used_todo_write else rounds_since_todo + 1
        if compact_requested:
            continue
