"""Collection and acknowledgement of runtime notifications.

Background completions and due cron prompts are asynchronous producers.  The
agent loop is their single consumer: it appends them to history immediately
before a model call (or beside tool results), then acknowledges any state that
is safe to remove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..features.background_tasks import (
    acknowledge_background_notifications,
    collect_background_results,
)
from ..features.cron_scheduler import collect_cron_notifications


@dataclass(frozen=True)
class RuntimeNotification:
    """One pending runtime event and its optional post-append acknowledgement."""

    source: str
    content: str
    acknowledge: Callable[[], None] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def collect_runtime_notifications() -> list[RuntimeNotification]:
    """Collect due cron prompts and completed background task notifications."""
    notifications: list[RuntimeNotification] = []

    for notification in collect_background_results():
        notifications.append(RuntimeNotification(
            source="background",
            content=notification.text,
            acknowledge=lambda notification=notification: acknowledge_background_notifications(
                [notification]
            ),
        ))

    notifications.extend(
        RuntimeNotification(
            source="cron",
            content=f"[Scheduled {notification.job_id}] {notification.prompt}",
            metadata={"cron_id": notification.job_id},
        )
        for notification in collect_cron_notifications()
    )
    return notifications


def notification_content(
    notifications: list[RuntimeNotification],
) -> list[dict[str, Any]]:
    """Format notifications as text content blocks for one user message."""
    return [{"type": "text", "text": item.content} for item in notifications]


def acknowledge_runtime_notifications(
    notifications: list[RuntimeNotification],
) -> None:
    """Acknowledge only notifications that have already been appended to history."""
    for notification in notifications:
        if notification.acknowledge is not None:
            notification.acknowledge()


def append_runtime_notifications(
    messages: list,
    notifications: list[RuntimeNotification],
    *,
    on_appended: Callable[[dict], None] | None = None,
) -> bool:
    """Append, journal, then acknowledge runtime notifications as one message."""
    if not notifications:
        return False
    message = {"role": "user", "content": notification_content(notifications)}
    messages.append(message)
    if on_appended is not None:
        on_appended(message)
    acknowledge_runtime_notifications(notifications)
    return True
