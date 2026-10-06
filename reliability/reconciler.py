"""Resolves writes whose outcome is unknown (F2) by reading back the idempotency key (T15-T17, I7)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from gateway.tools import ToolGateway
from reliability.clock import Clock
from reliability.events import EventSink
from reliability.executor import Executor
from reliability.idempotency import IntentLog

Verdict = Literal["applied", "not_applied", "unavailable"]


@dataclass
class Reconciliation:
    verdict: Verdict
    booking: dict[str, Any] | None = None
    reads: int = 0


class Reconciler:
    def __init__(self, gateway: ToolGateway, executor: Executor, intents: IntentLog,
                 policy: dict[str, Any], clock: Clock, sink: EventSink):
        self.gw, self.ex, self.intents = gateway, executor, intents
        self.cfg, self.clock, self.sink = policy["reconcile"], clock, sink

    def reconcile(self, appointment_id: str, key: str, op: str) -> Reconciliation:
        with self.ex.tracer.observe("reconcile-write", input={"op": op, "idempotency_key": key},
                                    metadata={"appointment_id": appointment_id}) as obs:
            result = self._reconcile(appointment_id, key, op)
            obs.update(output={"verdict": result.verdict, "reads": result.reads},
                       level="DEFAULT" if result.verdict != "unavailable" else "ERROR")
        return result

    def _reconcile(self, appointment_id: str, key: str, op: str) -> Reconciliation:
        self.intents.mark(key, "UNKNOWN")
        self.sink.emit("reconcile.start", appointment_id=appointment_id, idempotency_key=key, op=op,
                       trace_id=self.ex.trace_id)
        start, reads, absent = self.clock.now(), 0, 0
        result = Reconciliation("unavailable")
        while reads < self.cfg["max_reads"] and (self.clock.now() - start).total_seconds() < self.cfg["max_seconds"]:
            reads += 1
            r = self.ex.call(appointment_id, "get_by_key", lambda: self.gw.get_by_key(key))
            if not r.ok:
                continue
            body = r.result.body
            if body.get("found") and body.get("op") == op:
                result = Reconciliation("applied", body["booking"], reads)
                break
            absent += 1
            if absent >= self.cfg["absent_confirmations"]:
                result = Reconciliation("not_applied", None, reads)
                break
        result.reads = reads
        if result.verdict == "applied":
            self.intents.mark(key, "RESOLVED_SUCCESS", result.booking)
        elif result.verdict == "not_applied":
            self.intents.mark(key, "RESOLVED_ABSENT")
        self.sink.emit("reconcile.resolved", appointment_id=appointment_id, idempotency_key=key, op=op,
                       result=result.verdict, reads=reads, trace_id=self.ex.trace_id)
        return result
