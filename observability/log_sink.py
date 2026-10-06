"""EventSink that writes every reliability event (RELIABILITY_SPEC §7) to the daily pipeline log."""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("pipeline.events")

ERROR_EVENTS = {"escalation.packet", "budget.exhausted", "circuit.open", "orphan_hold"}
WARNING_EVENTS = {"failure.classified", "retry.scheduled", "transition.rejected", "conflict.detected",
                  "reconcile.start", "duplicate.detected", "compensation.run", "patient.nudge_due"}


def _fmt(name: str, a: dict[str, Any]) -> str:
    if name == "state.transition":
        return f"state {a['from_state']} -> {a['to_state']} ({a.get('transition')}, {a['event']})" + (
            f" cause={a['cause']}" if a.get("cause") else "")
    if name == "tool.call":
        return (f"tool {a['tool']} attempt={a['attempt']} status={a.get('status')} "
                f"error={a.get('error')} class={a['outcome_class']}")
    if name == "saga.step":
        return f"saga {a.get('step')} -> {a.get('status')}"
    skip = {"trace_id", "appointment_id"}
    return f"{name} " + " ".join(f"{k}={v}" for k, v in a.items() if k not in skip and v is not None)


class LogSink:
    def emit(self, name: str, **attrs: Any) -> None:
        level = logging.ERROR if name in ERROR_EVENTS else logging.WARNING if name in WARNING_EVENTS else logging.INFO
        if name == "saga.step" and attrs.get("status") not in ("ok", "SENT"):
            level = logging.WARNING
        ctx = f"trace={attrs.get('trace_id') or '-'} appt={attrs.get('appointment_id') or '-'}"
        log.log(level, "%s | %s", ctx, _fmt(name, attrs))
