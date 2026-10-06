"""Failure classification and retry/circuit policy (RELIABILITY_SPEC §3).

classify() is a pure function of the ToolResult, so it is unit-testable without any I/O.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from gateway.tools import ToolResult

POLICY_FILE = Path(__file__).with_name("policy.yaml")


class FailureClass(StrEnum):
    OK = "OK"
    F1_TRANSIENT_READ = "F1"
    F2_WRITE_UNKNOWN = "F2"
    F3_CONFLICT = "F3"
    F4_VALIDATION = "F4"
    F5_PARTIAL = "F5"
    F6_BUDGET = "F6"
    F7_DUPLICATE = "F7"
    F8_PATIENT_CHANGE = "F8"
    F9_AGENT_ERROR = "F9"
    F10_DEPENDENCY_DOWN = "F10"


def classify(r: ToolResult) -> FailureClass:
    if r.ok:
        return FailureClass.OK
    if r.error is not None or (r.status is not None and r.status >= 500):
        # No trustworthy response. Reads are safe to repeat; writes are of unknown outcome.
        return FailureClass.F2_WRITE_UNKNOWN if r.is_write else FailureClass.F1_TRANSIENT_READ
    if r.status == 409:
        return FailureClass.F3_CONFLICT
    return FailureClass.F4_VALIDATION


def load_policy(path: Path | None = None, **overrides: Any) -> dict[str, Any]:
    p = yaml.safe_load((path or POLICY_FILE).read_text(encoding="utf-8"))
    for dotted, val in overrides.items():
        node = p
        *parents, leaf = dotted.split("__")
        for k in parents:
            node = node[k]
        node[leaf] = val
    return p


def backoff(policy: dict[str, Any], attempt: int, rng: random.Random) -> float:
    """Delay before retry number `attempt` (1-based), with +/- jitter."""
    steps = policy["reads"]["backoff_s"]
    base = steps[min(attempt - 1, len(steps) - 1)]
    j = policy["reads"]["jitter"]
    return round(base * (1 + rng.uniform(-j, j)), 3)


@dataclass
class CircuitBreaker:
    """Per-tool breaker (F10): opens after N consecutive failures in the window."""
    threshold: int
    window_s: float
    half_open_after_s: float
    failures: list[datetime] = field(default_factory=list)
    opened_at: datetime | None = None

    def allow(self, now: datetime) -> bool:
        if self.opened_at is None:
            return True
        return now - self.opened_at >= timedelta(seconds=self.half_open_after_s)  # half-open probe

    def record(self, ok: bool, now: datetime) -> bool:
        """Returns True if this call just opened the circuit."""
        if ok:
            self.failures.clear()
            self.opened_at = None
            return False
        cutoff = now - timedelta(seconds=self.window_s)
        self.failures = [t for t in self.failures if t >= cutoff] + [now]
        if len(self.failures) >= self.threshold:
            was_closed = self.opened_at is None
            self.opened_at = now
            return was_closed
        return False

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None
