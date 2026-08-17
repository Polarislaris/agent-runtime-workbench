"""s08 context compaction pipeline."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .client import get_client
from ..config import (
    KEEP_RECENT_TOOL_RESULTS,
    MAX_MESSAGES_BEFORE_SNIP,
    MODEL,
    TOOL_OUTPUTS_DIR,
    TOOL_RESULT_BUDGET_BYTES,
    TOOL_RESULT_PREVIEW_CHARS,
    TRANSCRIPTS_DIR,
    WORKDIR,
)
from .messages import extract_text, serialize_message


def collect_tool_result_blocks(messages: list) -> list[tuple[int, int, dict]]:
    """Collect tool_result blocks for micro-compaction."""
    found = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                found.append((message_index, block_index, block))
    return found


def snip_compact(messages: list, max_messages: int = MAX_MESSAGES_BEFORE_SNIP) -> list:
    """L1: keep the head and tail when message count grows too large."""
    if len(messages) <= max_messages:
        return messages

    keep_head = 3
    keep_tail = max_messages - keep_head
    snipped = len(messages) - keep_head - keep_tail
    placeholder = {
        "role": "user",
        "content": f"[snipped {snipped} messages from conversation middle]",
    }
    print(f"\033[90m[compact L1] snipped {snipped} middle messages\033[0m")
    return messages[:keep_head] + [placeholder] + messages[-keep_tail:]


def micro_compact(messages: list) -> list:
    """L2: replace older large tool results with placeholders."""
    tool_results = collect_tool_result_blocks(messages)
    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return messages

    compacted = 0
    for _, _, block in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        content = str(block.get("content", ""))
        if len(content) > 120 and not content.startswith("[Earlier tool result compacted"):
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
            compacted += 1

    if compacted:
        print(f"\033[90m[compact L2] replaced {compacted} old tool results\033[0m")
    return messages


def persisted_output_path(tool_use_id: str, content: str) -> Path:
    """Create a stable, safe path for persisted large tool output."""
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(tool_use_id or "tool_result"))
    suffix = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()[:12]
    return TOOL_OUTPUTS_DIR / f"{safe_id}-{suffix}.txt"


def persist_large_output(tool_use_id: str, content: str) -> str:
    """Write large tool output to disk and return a small reference preview."""
    TOOL_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    path = persisted_output_path(tool_use_id, content)
    path.write_text(content, encoding="utf-8", errors="replace")
    try:
        rel_path = path.relative_to(WORKDIR)
    except ValueError:
        rel_path = path
    preview = content[:TOOL_RESULT_PREVIEW_CHARS]
    return (
        f"<persisted-output path=\"{rel_path}\" bytes=\"{len(content.encode('utf-8'))}\">\n"
        f"Full tool output was persisted to {rel_path}.\n"
        "Preview:\n"
        f"{preview}\n"
        "</persisted-output>"
    )


def tool_result_budget(messages: list, max_bytes: int = TOOL_RESULT_BUDGET_BYTES) -> list:
    """L3: persist large tool results from the latest tool-result turn."""
    if not messages:
        return messages

    last = messages[-1]
    content = last.get("content")
    if not isinstance(content, list):
        return messages

    blocks = [
        (index, block)
        for index, block in enumerate(content)
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    total = sum(len(str(block.get("content", "")).encode("utf-8")) for _, block in blocks)
    if total <= max_bytes:
        return messages

    persisted = 0
    ranked = sorted(
        blocks,
        key=lambda pair: len(str(pair[1].get("content", "")).encode("utf-8")),
        reverse=True,
    )
    for _, block in ranked:
        if total <= max_bytes:
            break

        current = str(block.get("content", ""))
        if current.startswith("<persisted-output "):
            continue

        original_size = len(current.encode("utf-8"))
        block["content"] = persist_large_output(block.get("tool_use_id", "tool_result"), current)
        persisted += 1
        total = total - original_size + len(str(block["content"]).encode("utf-8"))

    if persisted:
        print(f"\033[90m[compact L3] persisted {persisted} large tool results\033[0m")
    return messages


def write_transcript(messages: list) -> Path:
    """Persist full history before L4/reactive compaction replaces it."""
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPTS_DIR / f"transcript-{int(time.time() * 1000)}.jsonl"
    with path.open("w", encoding="utf-8") as file:
        for message in messages:
            file.write(json.dumps(serialize_message(message), ensure_ascii=False, default=str))
            file.write("\n")
    return path


def summarize_history(messages: list) -> str:
    """Ask the model for an operational continuation summary."""
    compact_prompt = (
        "Summarize this agent conversation for continuation. Preserve: current goal, "
        "user constraints, completed work, files changed, important findings, persisted "
        "output paths, errors, and next steps. Be concise but operational.\n\n"
        f"{json.dumps([serialize_message(m) for m in messages], ensure_ascii=False, default=str)}"
    )
    try:
        response = get_client().messages.create(
            model=MODEL,
            system="You summarize coding-agent conversations for compacted continuation.",
            messages=[{"role": "user", "content": compact_prompt}],
            max_tokens=4000,
        )
        return extract_text(response.content)
    except Exception as e:
        tail = [serialize_message(message) for message in messages[-5:]]
        return (
            f"Summary generation failed: {e}\n"
            "Fallback context: keep working from these recent messages:\n"
            f"{json.dumps(tail, ensure_ascii=False, default=str)[:8000]}"
        )


def compact_history(messages: list, label: str = "Compacted") -> list:
    """L4: replace full history with a transcript pointer and summary."""
    transcript_path = write_transcript(messages)
    summary = summarize_history(messages)
    rel_path = transcript_path.relative_to(WORKDIR)
    print(f"\033[90m[auto compact] wrote transcript to {rel_path}\033[0m")
    return [{
        "role": "user",
        "content": f"[{label}]\nTranscript: {rel_path}\n\n{summary}",
    }]


def reactive_compact(messages: list) -> list:
    """Emergency compaction after the API reports the prompt is still too long."""
    transcript_path = write_transcript(messages)
    summary = summarize_history(messages)
    tail = messages[-5:]
    rel_path = transcript_path.relative_to(WORKDIR)
    print(f"\033[90m[reactive compact] wrote transcript to {rel_path}\033[0m")
    return [{
        "role": "user",
        "content": f"[Reactive compact]\nTranscript: {rel_path}\n\n{summary}",
    }, *tail]


def is_prompt_too_long_error(error: Exception) -> bool:
    """Recognize common gateway/SDK context-length errors."""
    text = f"{type(error).__name__}: {error}".lower()
    needles = [
        "prompt_too_long",
        "prompt too long",
        "context length",
        "maximum context",
        "413",
        "request too large",
    ]
    return any(needle in text for needle in needles)

