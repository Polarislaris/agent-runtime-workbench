"""HTTP-facing data models shared by later Agent Runtime API routes."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator


RunStatus = Literal[
    "queued",
    "running",
    "waiting_permission",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]


class CreateRunRequest(BaseModel):
    """Validated payload for starting one in-memory Agent run."""

    prompt: str

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("prompt must not be empty")
        return normalized


class PermissionDecisionRequest(BaseModel):
    """Validated browser decision for one pending tool permission."""

    decision: Literal["allow", "deny"]


class RunSnapshot(BaseModel):
    """Detached, JSON-safe view of mutable in-memory run state."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    status: RunStatus
    messages: list[dict[str, Any]]
    events: list[dict[str, Any]]
    started_at: str
    completed_at: Optional[str] = None
    error: Optional[str] = None
    last_sequence: int = 0
