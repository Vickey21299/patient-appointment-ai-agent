"""Applies the policy to every tool call: classify, bounded read retries, circuit breaker, budget."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Callable

from gateway.tools import WRITE_TOOLS, ToolResult
from reliability.clock import Clock
from reliability.events import EventSink, NullTracer, Tracer
from reliability.policy import CircuitBreaker, FailureClass, backoff, classify
from storage.db import tx

FC = FailureClass


@dataclass
class Exec:
    tool: str
    cls: FailureClass
    result: ToolResult | None      # None if the call was never sent (circuit open)
    attempts: int
    budget_exhausted: bool = False

    @property
    def ok(self) -> bool:
        return self.cls is FC.OK


class Executor:
    def __init__(self, db_path: str, policy: dict[str, Any], clock: Clock, sink: EventSink, seed: int = 7,
                 tracer: Tracer | None = None):
        self.db_path, self.policy, self.clock, self.sink = db_path, policy, clock, sink
        self.tracer = tracer or NullTracer()
        self.rng = random.Random(seed)   # seeded so retry timing is reproducible across runs
        cb = policy["circuit_breaker"]
        self._cb_cfg = (cb["failure_threshold"], cb["window_s"], cb["half_open_after_s"])
        self.breakers: dict[str, CircuitBreaker] = {}
        self.trace_id: str | None = None

    def breaker(self, tool: str) -> CircuitBreaker:
        if tool not in self.breakers:
            self.breakers[tool] = CircuitBreaker(*self._cb_cfg)
        return self.breakers[tool]

    def call(self, appointment_id: str | None, tool: str, fn: Callable[[], ToolResult],
             idempotency_key: str | None = None, trace_input: Any = None) -> Exec:
        """One traced `tool` observation per logical call; retry attempts and failure events nest inside it."""
        with self.tracer.observe(tool.replace("_", "-"), as_type="tool", input=trace_input, metadata={
                "appointment_id": appointment_id, "idempotency_key": idempotency_key,
                "is_write": tool in WRITE_TOOLS}) as obs:
            ex = self._call(appointment_id, tool, fn, idempotency_key)
            res = ex.result
            obs.update(
                output={"outcome_class": str(ex.cls), "attempts": ex.attempts,
                        "status": res.status if res else None, "error": res.error if res else "circuit_open",
                        "body": res.body if res else None},
                level="DEFAULT" if ex.ok else ("WARNING" if ex.cls in (FC.F3_CONFLICT, FC.F4_VALIDATION) else "ERROR"),
                status_message=None if ex.ok else f"{ex.cls} after {ex.attempts} attempt(s)",
            )
        return ex

    def _call(self, appointment_id: str | None, tool: str, fn: Callable[[], ToolResult],
              idempotency_key: str | None) -> Exec:
        is_write = tool in WRITE_TOOLS
        # Reads retry on F1. Writes get exactly one attempt per call; I7 is enforced by the caller reconciling.
        max_attempts = 1 if is_write else self.policy["reads"]["max_attempts"]
        br = self.breaker(tool)
        attempt, cls, res = 0, FC.OK, None
        while attempt < max_attempts:
            attempt += 1
            if not br.allow(self.clock.now()):
                cls, res = FC.F10_DEPENDENCY_DOWN, None
                self._emit_failure(appointment_id, tool, cls, attempt, "circuit_open")
                break
            res = fn()
            cls = classify(res)
            self.sink.emit("tool.call", appointment_id=appointment_id, tool=tool, attempt=attempt,
                           idempotency_key=idempotency_key, status=res.status, error=res.error,
                           outcome_class=str(cls), trace_id=self.trace_id)
            transport_fail = cls in (FC.F1_TRANSIENT_READ, FC.F2_WRITE_UNKNOWN)
            if br.record(not transport_fail, self.clock.now()):
                self.sink.emit("circuit.open", tool=tool, trace_id=self.trace_id, failure_class="F10")
            if cls is FC.OK:
                break
            self._emit_failure(appointment_id, tool, cls, attempt, res.error or res.error_code or str(res.status))
            if cls is FC.F1_TRANSIENT_READ and attempt < max_attempts:
                delay = backoff(self.policy, attempt, self.rng)
                self.sink.emit("retry.scheduled", appointment_id=appointment_id, tool=tool,
                               attempt=attempt + 1, backoff_ms=int(delay * 1000), trace_id=self.trace_id)
                self.clock.sleep(delay)
                continue
            break
        if cls in (FC.F1_TRANSIENT_READ, FC.F2_WRITE_UNKNOWN) and br.is_open:
            cls = FC.F10_DEPENDENCY_DOWN if cls is FC.F1_TRANSIENT_READ else cls
        return Exec(tool, cls, res, attempt, self._charge_budget(appointment_id, cls))

    def _emit_failure(self, appointment_id, tool, cls, attempt, detail) -> None:
        self.sink.emit("failure.classified", appointment_id=appointment_id, tool=tool, failure_class=str(cls),
                       attempt=attempt, detail=detail, trace_id=self.trace_id)

    def _charge_budget(self, appointment_id: str | None, cls: FailureClass) -> bool:
        """F6: count failed calls per appointment; True once the budget is exhausted."""
        if appointment_id is None or cls in (FC.OK, FC.F3_CONFLICT):
            return False
        with tx(self.db_path) as c:
            c.execute("UPDATE appointments SET failed_calls = failed_calls + 1 WHERE id=?", (appointment_id,))
            n = c.execute("SELECT failed_calls FROM appointments WHERE id=?", (appointment_id,)).fetchone()[0]
        exhausted = n >= self.policy["budget"]["max_failed_calls_per_appointment"]
        if exhausted:
            self.sink.emit("budget.exhausted", appointment_id=appointment_id, failed_calls=n,
                           failure_class="F6", trace_id=self.trace_id)
        return exhausted
