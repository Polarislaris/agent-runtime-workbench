from __future__ import annotations

import importlib


class FakeStatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def test_retry_delay_uses_configured_caps(monkeypatch):
    recovery = importlib.import_module("agent.runtime.error_recovery")

    assert recovery.retry_delay(0, rng=lambda _a, _b: 0) == 0.5
    assert recovery.retry_delay(10, rng=lambda _a, _b: 0) == 8.0


def test_with_retry_retries_429_then_succeeds():
    recovery = importlib.import_module("agent.runtime.error_recovery")
    state = recovery.RecoveryState(current_model="primary")
    calls = []

    def request(model):
        calls.append(model)
        if len(calls) == 1:
            raise FakeStatusError(429)
        return "ok"

    result = recovery.with_retry(
        request,
        state,
        max_retries=2,
        sleep_fn=lambda _delay: None,
    )

    assert result == "ok"
    assert calls == ["primary", "primary"]


def test_with_retry_reports_a_safe_retry_schedule():
    recovery = importlib.import_module("agent.runtime.error_recovery")
    state = recovery.RecoveryState(current_model="primary")
    schedules = []
    calls = []

    def request(_model):
        calls.append(1)
        if len(calls) == 1:
            raise FakeStatusError(429)
        return "ok"

    assert recovery.with_retry(
        request,
        state,
        max_retries=2,
        sleep_fn=lambda _delay: None,
        on_retry=schedules.append,
    ) == "ok"
    assert len(schedules) == 1
    assert schedules[0]["status_code"] == 429
    assert schedules[0]["attempt"] == 1
    assert schedules[0]["max_retries"] == 2
    assert 500 <= schedules[0]["delay_ms"] <= 625
    assert schedules[0]["model"] == "primary"
    assert schedules[0]["fallback_active"] is False


def test_repeated_529_switches_to_fallback(monkeypatch):
    recovery = importlib.import_module("agent.runtime.error_recovery")
    monkeypatch.setattr(recovery, "FALLBACK_MODEL", "fallback")
    state = recovery.RecoveryState(current_model="primary")
    calls = []

    def request(model):
        calls.append(model)
        if len(calls) <= 3:
            raise FakeStatusError(529)
        return model

    result = recovery.with_retry(
        request,
        state,
        max_retries=4,
        sleep_fn=lambda _delay: None,
    )

    assert result == "fallback"
    assert calls == ["primary", "primary", "primary", "fallback"]


def test_output_escalation_only_happens_once():
    recovery = importlib.import_module("agent.runtime.error_recovery")
    state = recovery.RecoveryState()

    assert recovery.should_escalate_output_tokens(state)
    recovery.escalate_output_tokens(state)
    assert state.max_tokens == recovery.ESCALATED_MAX_TOKENS
    assert not recovery.should_escalate_output_tokens(state)
