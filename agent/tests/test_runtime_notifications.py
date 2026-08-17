"""Tests for the s20 runtime-notification injection boundary."""

from __future__ import annotations

from agent.runtime.notifications import (
    RuntimeNotification,
    append_runtime_notifications,
    notification_content,
)


def test_append_runtime_notifications_appends_before_acknowledging() -> None:
    """Keep background cleanup strictly after the notification is in history."""
    messages: list[dict] = []
    observed_history_lengths: list[int] = []

    notification = RuntimeNotification(
        source="background",
        content="<task_notification>build finished</task_notification>",
        acknowledge=lambda: observed_history_lengths.append(len(messages)),
    )

    assert append_runtime_notifications(messages, [notification]) is True
    assert observed_history_lengths == [1]
    assert messages == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "<task_notification>build finished</task_notification>",
                }
            ],
        }
    ]


def test_empty_notification_batch_does_not_change_history() -> None:
    """Avoid creating empty user messages when no runtime event is pending."""
    messages = [{"role": "user", "content": "original prompt"}]

    assert append_runtime_notifications(messages, []) is False
    assert messages == [{"role": "user", "content": "original prompt"}]
    assert notification_content([]) == []
