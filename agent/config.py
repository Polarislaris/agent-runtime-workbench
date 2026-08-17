"""Typed runtime configuration for the standalone Agent project.

Values that change between machines or deployments live in the repository-root
``.env`` file. This module deliberately keeps path derivation, defaults, and
numeric validation in Python so the rest of the Agent never has to parse an
untrusted environment string itself.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


# Load this project only; ``override=False`` lets an explicit shell/CI variable
# take precedence over .env without requiring a source-code change.
PROJECT_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = PROJECT_ROOT.parent
ENV_FILE = REPOSITORY_ROOT / ".env"
load_dotenv(ENV_FILE, override=False)


def _env_text(name: str, default: str) -> str:
    """Read a non-blank text setting while retaining a documented default."""
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """Read a bounded integer early, so malformed .env files fail clearly."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from error
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}, got {value}")
    return value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    """Read a bounded decimal setting, used for polling and timeout intervals."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from error
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}, got {value}")
    return value


def _env_path(name: str, default: Path) -> Path:
    """Expand and resolve an optional path setting relative to the host machine."""
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else default.expanduser().resolve()


# Source-code location is intrinsic to this checkout; the business workspace is
# intentionally environment-specific so one Agent can operate on many projects.
DEFAULT_WORKDIR = Path("/Users/yoyo-mac/Documents/project/demo-app")
WORKDIR = _env_path("AGENT_WORKDIR", DEFAULT_WORKDIR)
STATE_DIR = _env_path("AGENT_STATE_DIR", WORKDIR / ".agent")
DATABASE_DIR = STATE_DIR / "database"
WORKTREES_DIR = _env_path("AGENT_WORKTREES_DIR", WORKDIR / ".worktrees")
SKILLS_DIR = PROJECT_ROOT / "skills"

# Model gateway credentials and model names belong in .env. API keys are never
# committed and are not stored in the Agent's SQLite databases.
MODEL = _env_text("MODEL", "qwen3.7-plus")
FALLBACK_MODEL = _env_text("FALLBACK_MODEL", "qwen3.7-max")
ANTHROPIC_API_KEY = _env_text("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = _env_text(
    "ANTHROPIC_BASE_URL",
    "https://dashscope.aliyuncs.com/apps/anthropic",
)

# Derived durable paths stay in code: changing STATE_DIR updates every related
# store consistently instead of requiring multiple duplicated .env entries.
TASKS_DIR = STATE_DIR / "legacy_tasks"
SCHEDULED_TASKS_FILE = STATE_DIR / "scheduled_tasks.json"
TEAM_DB = DATABASE_DIR / "team.sqlite3"
MEMORY_DIR = STATE_DIR / "memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
TRANSCRIPTS_DIR = STATE_DIR / "transcripts"
TOOL_OUTPUTS_DIR = STATE_DIR / "task_outputs" / "tool-results"
RUN_DB = _env_path("AGENT_RUN_DB", DATABASE_DIR / "runs.sqlite3")

# Runtime behavior is configurable because local demos and production-like runs
# need different limits. MEMORY_TYPES remains code because it is an invariant
# schema/validation set, not a deployment preference.
CRON_SCHEDULER_INTERVAL_SECONDS = _env_float("CRON_SCHEDULER_INTERVAL_SECONDS", 1.0)
CRON_QUEUE_PROCESSOR_INTERVAL_SECONDS = _env_float("CRON_QUEUE_PROCESSOR_INTERVAL_SECONDS", 0.2)
TEAM_AGENT_ID = _env_text("TEAM_AGENT_ID", "lead")
MAX_TEAMMATES = _env_int("MAX_TEAMMATES", 4, 1)
TEAM_INBOX_LIMIT = _env_int("TEAM_INBOX_LIMIT", 50, 1)
TEAM_CLAIM_TIMEOUT_SECONDS = _env_int("TEAM_CLAIM_TIMEOUT_SECONDS", 300, 1)
TEAMMATE_MAX_TURNS = _env_int("TEAMMATE_MAX_TURNS", 10, 1)
TEAM_IDLE_POLL_SECONDS = _env_float("TEAM_IDLE_POLL_SECONDS", 1.0)
TEAM_IDLE_TIMEOUT_SECONDS = _env_int("TEAM_IDLE_TIMEOUT_SECONDS", 60, 1)
AUTONOMOUS_TASK_SCAN_LIMIT = _env_int("AUTONOMOUS_TASK_SCAN_LIMIT", 5, 1)
PROTOCOL_REQUEST_TIMEOUT_SECONDS = _env_int("PROTOCOL_REQUEST_TIMEOUT_SECONDS", 300, 1)

MEMORY_TYPES = {"user", "feedback", "project", "reference"}
MAX_LOADED_MEMORIES = _env_int("MAX_LOADED_MEMORIES", 5, 1)
CONSOLIDATE_THRESHOLD = _env_int("CONSOLIDATE_THRESHOLD", 10, 1)
MAX_MESSAGES_BEFORE_SNIP = _env_int("MAX_MESSAGES_BEFORE_SNIP", 50, 1)
KEEP_RECENT_TOOL_RESULTS = _env_int("KEEP_RECENT_TOOL_RESULTS", 3, 0)
TOOL_RESULT_BUDGET_BYTES = _env_int("TOOL_RESULT_BUDGET_BYTES", 200_000, 1)
TOOL_RESULT_PREVIEW_CHARS = _env_int("TOOL_RESULT_PREVIEW_CHARS", 2_000, 1)
COMPACT_TOKEN_THRESHOLD = _env_int("COMPACT_TOKEN_THRESHOLD", 80_000, 1)
MAX_REACTIVE_RETRIES = _env_int("MAX_REACTIVE_RETRIES", 1, 0)

DEFAULT_MAX_TOKENS = _env_int("DEFAULT_MAX_TOKENS", 8_000, 1)
ESCALATED_MAX_TOKENS = _env_int("ESCALATED_MAX_TOKENS", 64_000, 1)
MAX_CONTINUATION_RETRIES = _env_int("MAX_CONTINUATION_RETRIES", 3, 0)
MAX_RETRIES = _env_int("MAX_RETRIES", 5, 0)
BASE_DELAY_MS = _env_int("BASE_DELAY_MS", 500, 0)
MAX_DELAY_MS = _env_int("MAX_DELAY_MS", 8_000, 1)

MAX_BACKGROUND_TASKS = _env_int("MAX_BACKGROUND_TASKS", 4, 1)
BACKGROUND_RESULT_PREVIEW_CHARS = _env_int("BACKGROUND_RESULT_PREVIEW_CHARS", 2_000, 1)
WEB_PERMISSION_TIMEOUT_SECONDS = _env_float("WEB_PERMISSION_TIMEOUT_SECONDS", 60.0, 1.0)
RUN_EVENT_RETENTION_DAYS = _env_int("RUN_EVENT_RETENTION_DAYS", 30, 1)
RUN_EVENT_PREVIEW_CHARS = _env_int("RUN_EVENT_PREVIEW_CHARS", 2_000, 200)
