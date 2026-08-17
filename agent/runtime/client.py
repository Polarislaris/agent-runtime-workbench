"""Lazy Anthropic client creation.

Keeping client construction behind a function makes tests and future provider
swaps simpler: modules can import get_client() without opening a connection or
constructing SDK objects until the first model call.
"""

from __future__ import annotations

from anthropic import Anthropic

from ..config import ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL


_CLIENT: Anthropic | None = None


def get_client() -> Anthropic:
    """Return a process-wide client instance."""
    global _CLIENT
    if _CLIENT is None:
        if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == "PASTE_YOUR_API_KEY_HERE":
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not configured. "
                "Set it in the repository-root .env file and restart the backend."
            )
        _CLIENT = Anthropic(
            api_key=ANTHROPIC_API_KEY,
            base_url=ANTHROPIC_BASE_URL,
        )
    return _CLIENT
