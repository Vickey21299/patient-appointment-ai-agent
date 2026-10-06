import random
from datetime import timedelta

import pytest

from gateway.tools import ToolResult
from reliability.clock import FakeClock
from reliability.idempotency import make_key
from reliability.policy import CircuitBreaker, FailureClass as FC, backoff, classify, load_policy


@pytest.mark.parametrize("tool,status,error,expected", [
    ("search_slots", 200, None, FC.OK),
    ("search_slots", 503, None, FC.F1_TRANSIENT_READ),
    ("search_slots", None, "timeout", FC.F1_TRANSIENT_READ),
    ("book_appointment", None, "timeout", FC.F2_WRITE_UNKNOWN),
    ("book_appointment", 503, None, FC.F2_WRITE_UNKNOWN),
    ("book_appointment", None, "connect", FC.F2_WRITE_UNKNOWN),
    ("hold_slot", 409, None, FC.F3_CONFLICT),
    ("book_appointment", 400, None, FC.F4_VALIDATION),
    ("verify_patient", 404, None, FC.F4_VALIDATION),
])
def test_classification_table(tool, status, error, expected):
    assert classify(ToolResult(tool, status=status, error=error)) is expected


def test_backoff_follows_schedule_within_jitter():
    p = load_policy()
    rng = random.Random(1)
    for attempt, base in enumerate(p["reads"]["backoff_s"], start=1):
        d = backoff(p, attempt, rng)
        assert base * 0.75 <= d <= base * 1.25


def test_policy_overrides():
    assert load_policy(reads__max_attempts=5)["reads"]["max_attempts"] == 5


def test_circuit_breaker_opens_and_half_opens():
    clock = FakeClock()
    cb = CircuitBreaker(threshold=3, window_s=60, half_open_after_s=30)
    assert not cb.record(False, clock.now()) and not cb.record(False, clock.now())
    assert cb.record(False, clock.now()) is True
    assert not cb.allow(clock.now())
    clock.advance(seconds=31)
    assert cb.allow(clock.now())
    cb.record(True, clock.now())
    assert not cb.is_open


def test_breaker_window_forgets_old_failures():
    clock = FakeClock()
    cb = CircuitBreaker(threshold=2, window_s=60, half_open_after_s=30)
    cb.record(False, clock.now())
    clock.advance(seconds=61)
    assert cb.record(False, clock.now()) is False


def test_idempotency_key_is_deterministic_and_attempt_independent():
    k1 = make_key("apt_1", "book", {"hold_id": "h1"})
    assert k1 == make_key("apt_1", "book", {"hold_id": "h1"})
    assert k1 != make_key("apt_1", "book", {"hold_id": "h2"})
    assert k1.startswith("book-")
