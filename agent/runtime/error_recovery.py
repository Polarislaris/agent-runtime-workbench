"""s11 error recovery policies.

This module owns retry strategy and recovery state. The main loop still decides
where to continue because only the loop knows whether to retry the same request,
append a continuation prompt, or compact the active conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
import random
import time
from typing import Any, Callable, Optional

from ..config import (
    BASE_DELAY_MS,
    DEFAULT_MAX_TOKENS,
    ESCALATED_MAX_TOKENS,
    FALLBACK_MODEL,
    MAX_CONTINUATION_RETRIES,
    MAX_DELAY_MS,
    MAX_RETRIES,
    MODEL,
)


CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly from where you stopped. "
    "Do not apologize, do not recap, and do not repeat completed content."
)


@dataclass
class RecoveryState:
    """Mutable recovery state for one agent_loop invocation."""

    current_model: str = MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    has_escalated_output_tokens: bool = False
    continuation_count: int = 0
    has_attempted_reactive_compact: bool = False
    consecutive_529: int = 0
    has_switched_model: bool = False


def _status_code(error: Exception) -> Optional[int]:
    """Extract an HTTP-ish status code from SDK exceptions or text errors."""
    for attr in ("status_code", "status"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value

    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int):
        return value

    text = f"{type(error).__name__}: {error}".lower()
    if "429" in text or "rate_limit" in text or "rate limit" in text:
        return 429
    if "529" in text or "overloaded" in text or "overload" in text:
        return 529
    return None


def _retry_after_seconds(error: Exception) -> Optional[float]:
    """Read Retry-After when the SDK exposes response headers."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None

    value = None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_rate_limit_error(error: Exception) -> bool:
    """Return True for 429 rate-limit failures."""
    return _status_code(error) == 429


def is_overloaded_error(error: Exception) -> bool:
    """Return True for 529 overloaded failures."""
    return _status_code(error) == 529


def is_transient_error(error: Exception) -> bool:
    """Only retry transient API failures that are likely to clear soon."""
    return is_rate_limit_error(error) or is_overloaded_error(error)


def retry_delay(
    attempt: int,
    retry_after: Optional[float] = None,
    rng: Callable[[float, float], float] = random.uniform,
) -> float:
    """Compute bounded exponential backoff with 0-25% jitter."""
    if retry_after is not None:
        return max(0.0, retry_after)

    base_ms = min(BASE_DELAY_MS * (2 ** attempt), MAX_DELAY_MS)
    jitter_ms = rng(0, base_ms * 0.25)
    return (base_ms + jitter_ms) / 1000


def maybe_switch_fallback_model(state: RecoveryState) -> None:
    """Switch model after repeated overloads when a fallback is configured."""
    if state.consecutive_529 < 3:
        return
    if not FALLBACK_MODEL or state.has_switched_model:
        return

    print(
        f"\033[90m[recovery] switching model after repeated 529: "
        f"{state.current_model} -> {FALLBACK_MODEL}\033[0m"
    )
    state.current_model = FALLBACK_MODEL
    state.has_switched_model = True


def with_retry(
    request_fn: Callable[[str], object],
    state: RecoveryState,
    max_retries: int = MAX_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
    on_retry: Optional[Callable[[dict[str, Any]], None]] = None,
) -> object:
    """Run an API request with bounded retry for 429/529 transient failures.

    request_fn receives the current model id. That keeps provider/model choice in
    the strategy layer while the loop supplies the actual API call.
    """
    last_error: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            response = request_fn(state.current_model)
            state.consecutive_529 = 0
            return response
        except Exception as error:
            if not is_transient_error(error):
                raise

            last_error = error
            if is_overloaded_error(error):
                state.consecutive_529 += 1
                maybe_switch_fallback_model(state)
            else:
                state.consecutive_529 = 0

            delay = retry_delay(attempt, retry_after=_retry_after_seconds(error))
            if on_retry is not None:
                # The loop owns the event sink. The policy only reports a
                # compact decision record and never exposes SDK internals.
                on_retry({
                    "status_code": _status_code(error),
                    "attempt": attempt + 1,
                    "max_retries": max_retries,
                    "delay_ms": round(delay * 1_000),
                    "model": state.current_model,
                    "fallback_active": state.has_switched_model,
                })
            print(
                f"\033[90m[recovery] transient { _status_code(error) } "
                f"attempt {attempt + 1}/{max_retries}, retrying in {delay:.2f}s\033[0m"
            )
            sleep_fn(delay)

    if last_error is not None:
        raise last_error
    raise RuntimeError("Retry loop exited without a response or error")


def should_escalate_output_tokens(state: RecoveryState) -> bool:
    """First max_tokens stop retries the same request with a larger output cap."""
    return not state.has_escalated_output_tokens


def escalate_output_tokens(state: RecoveryState) -> None:
    """Increase the API output budget from the default to the escalated cap."""
    state.max_tokens = ESCALATED_MAX_TOKENS
    state.has_escalated_output_tokens = True
    print(
        f"\033[90m[max_tokens] escalating "
        f"{DEFAULT_MAX_TOKENS} -> {ESCALATED_MAX_TOKENS}\033[0m"
    )


def can_continue_after_truncation(state: RecoveryState) -> bool:
    """Allow only a few continuation turns to avoid runaway output."""
    return state.continuation_count < MAX_CONTINUATION_RETRIES


def record_continuation_request(state: RecoveryState) -> None:
    """Track continuation attempts after the escalated cap still truncates."""
    state.continuation_count += 1
    print(
        f"\033[90m[max_tokens] continuation "
        f"{state.continuation_count}/{MAX_CONTINUATION_RETRIES}\033[0m"
    )
