"""Helpers for message serialization, text extraction, and compact estimates."""

from __future__ import annotations

import json


def serialize_block(block):
    """Convert SDK content blocks or plain dict blocks into JSON-safe data."""
    if isinstance(block, dict):
        return block

    data = {}
    for attr in ("type", "id", "name", "input", "text"):
        if hasattr(block, attr):
            data[attr] = getattr(block, attr)
    return data or str(block)


def serialize_message(message: dict) -> dict:
    """Convert a chat message into a JSON-safe structure."""
    content = message.get("content")
    if isinstance(content, list):
        content = [serialize_block(block) for block in content]
    return {"role": message.get("role"), "content": content}


def extract_text(content) -> str:
    """Extract text blocks from Anthropic-style content."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    text = "\n".join(part for part in parts if part).strip()
    return text or "(no final text)"


def format_recent_messages(messages: list, max_chars: int = 4000) -> str:
    """Format recent dialogue for side-queries without including huge tool output."""
    lines = []
    for message in messages:
        role = message.get("role", "unknown")
        content = message.get("content", "")

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for block in content:
                data = serialize_block(block)
                block_type = data.get("type") if isinstance(data, dict) else None
                if block_type == "text":
                    parts.append(str(data.get("text", "")))
                elif block_type == "tool_use":
                    parts.append(f"[tool_use {data.get('name')} {data.get('input')}]")
                elif block_type == "tool_result":
                    preview = str(data.get("content", ""))[:300]
                    parts.append(f"[tool_result {preview}]")
            text = "\n".join(part for part in parts if part)
        else:
            text = str(content)

        if text.strip():
            lines.append(f"{role}: {text.strip()}")

    formatted = "\n\n".join(lines)
    return formatted[-max_chars:]


def estimate_token_count(messages: list, system_prompt: str, tools: list) -> int:
    """Teaching approximation: count roughly one token per four JSON chars."""
    payload = {
        "system": system_prompt,
        "tools": tools,
        "messages": [serialize_message(message) for message in messages],
    }
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return max(1, len(text) // 4)


def serialized_size(messages: list) -> int:
    """Estimate serialized message size for choosing the richest memory snapshot."""
    return len(json.dumps(messages, ensure_ascii=False, default=str))

