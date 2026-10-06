"""Appointment lifecycle state machine (RELIABILITY_SPEC §1-§2).

The LLM proposes events; this module decides whether they are legal. Every accepted
transition is written to `transitions` and emitted as `state.transition` (I10). Any
(state, event) pair not in TRANSITIONS is rejected (F9).
"""
from __future__ import annotations

import json
import uuid
from enum import StrEnum
from typing import Any, Callable

from reliability.clock import Clock, iso, parse
from reliability.events import EventSink
from storage.db import tx


class State(StrEnum):
    COLLECTING = "COLLECTING"
    SEARCHING = "SEARCHING"
    OFFERED = "OFFERED"
    HELD = "HELD"
    CONFIRMING = "CONFIRMING"
    COMMITTING = "COMMITTING"
    BOOKED = "BOOKED"
    MODIFYING = "MODIFYING"
    CANCELLING = "CANCELLING"
    RECONCILING = "RECONCILING"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    ESCALATED = "ESCALATED"


TERMINAL = {State.CANCELLED, State.EXPIRED, State.ESCALATED}
PRE_BOOKING = {State.COLLECTING, State.SEARCHING, State.OFFERED, State.HELD, State.CONFIRMING}

S = State
# A target is a State, or {op: State} when it depends on which write is being reconciled.
Target = State | dict[str, State]

TRANSITIONS: dict[tuple[State, str], tuple[str, Target]] = {
    (S.COLLECTING, "fields_complete"): ("T01", S.SEARCHING),
    (S.SEARCHING, "slots_found"): ("T02", S.OFFERED),
    (S.SEARCHING, "no_slots"): ("T03", S.COLLECTING),
    (S.OFFERED, "slot_chosen"): ("T04", S.HELD),
    (S.OFFERED, "results_stale"): ("T05", S.SEARCHING),
    (S.OFFERED, "preferences_changed"): ("T05", S.SEARCHING),
    (S.HELD, "hold_ok"): ("T06", S.CONFIRMING),
    (S.HELD, "hold_conflict"): ("T07", S.OFFERED),
    (S.HELD, "hold_failed"): ("T07", S.OFFERED),
    (S.CONFIRMING, "patient_yes"): ("T08", S.COMMITTING),
    (S.CONFIRMING, "hold_expired"): ("T09", S.OFFERED),
    (S.CONFIRMING, "patient_no"): ("T10", S.OFFERED),
    (S.CONFIRMING, "preferences_changed"): ("T10", S.OFFERED),
    (S.COMMITTING, "book_ok"): ("T11", S.BOOKED),
    (S.COMMITTING, "book_conflict"): ("T12", S.OFFERED),
    (S.COMMITTING, "book_unknown"): ("T13", S.RECONCILING),
    (S.COMMITTING, "book_invalid"): ("T14", S.COLLECTING),
    (S.RECONCILING, "readback_applied"): ("T15", {"book": S.BOOKED, "cancel": S.CANCELLED}),
    (S.RECONCILING, "retry_write"): ("T16", {"book": S.COMMITTING, "cancel": S.CANCELLING}),
    (S.RECONCILING, "readback_not_applied"): ("T16", {"book": S.OFFERED, "cancel": S.BOOKED}),
    (S.RECONCILING, "readback_unavailable"): ("T17", S.ESCALATED),
    (S.BOOKED, "reschedule_request"): ("T18", S.MODIFYING),
    (S.MODIFYING, "reschedule_done"): ("T19", S.BOOKED),
    (S.MODIFYING, "reschedule_aborted"): ("T20", S.BOOKED),
    (S.MODIFYING, "reschedule_stuck"): ("T21", S.ESCALATED),
    (S.BOOKED, "cancel_request"): ("T22", S.CANCELLING),
    (S.CANCELLING, "cancel_ok"): ("T23", S.CANCELLED),
    (S.CANCELLING, "cancel_unknown"): ("T24", S.RECONCILING),
    (S.CANCELLING, "cancel_rejected"): ("T24", S.BOOKED),
}
# T25: idle timeout / abandonment from any pre-booking state.
for _s in PRE_BOOKING:
    TRANSITIONS[(_s, "idle_timeout")] = ("T25", S.EXPIRED)
    TRANSITIONS[(_s, "patient_abandon")] = ("T25", S.EXPIRED)
# T26: budget exhaustion / safety flag from any non-terminal state.
for _s in State:
    if _s not in TERMINAL:
        TRANSITIONS[(_s, "escalate")] = ("T26", S.ESCALATED)


Guard = Callable[[dict[str, Any], dict[str, Any], Clock], str | None]


def _verified_and_complete(row, ctx, clock):
    if not row["patient_verified"]:
        return "I8: patient not verified"
    if not row["visit_type"]:
        return "visit_type missing"
    return None


def _hold_valid(row, ctx, clock):
    if not row["hold_id"] or not row["hold_expires_at"]:
        return "no hold"
    if parse(row["hold_expires_at"]) <= clock.now():
        return "hold expired"
    return None


GUARDS: dict[tuple[State, str], Guard] = {
    (S.COLLECTING, "fields_complete"): _verified_and_complete,
    (S.CONFIRMING, "patient_yes"): _hold_valid,  # T08
}


class IllegalTransition(Exception):
    def __init__(self, state: str, event: str, reason: str):
        super().__init__(f"{event} not allowed in {state}: {reason}")
        self.state, self.event, self.reason = state, event, reason


class StateMachine:
    def __init__(self, db_path: str, clock: Clock, sink: EventSink):
        self.db_path, self.clock, self.sink = db_path, clock, sink

    def create(self, trace_id: str | None = None) -> str:
        appt_id = f"apt_{uuid.uuid4().hex[:10]}"
        now = iso(self.clock.now())
        with tx(self.db_path) as c:
            c.execute(
                "INSERT INTO appointments(id, state, last_activity_at, created_at, updated_at)"
                " VALUES (?,?,?,?,?)",
                (appt_id, S.COLLECTING, now, now, now),
            )
        self.sink.emit("appointment.created", appointment_id=appt_id, trace_id=trace_id)
        return appt_id

    def apply(
        self,
        appointment_id: str,
        event: str,
        *,
        cause: str = "",
        trace_id: str | None = None,
        op: str | None = None,
        updates: dict[str, Any] | None = None,
        expected_version: int | None = None,
        activity: bool = True,
    ) -> dict[str, Any]:
        """Validate and apply one transition atomically, together with any column `updates`.

        activity=False for system-driven transitions (timeouts), so they don't reset the
        patient idle clock (T25).
        """
        with tx(self.db_path) as c:
            row = c.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
            if row is None:
                raise KeyError(appointment_id)
            row = dict(row)
            cur = State(row["state"])
            reason = None
            spec = TRANSITIONS.get((cur, event))
            if spec is None:
                reason = "no such transition"
            elif expected_version is not None and row["version"] != expected_version:
                reason = f"version conflict ({row['version']} != {expected_version})"
            else:
                guard = GUARDS.get((cur, event))
                reason = guard(row, {"op": op}, self.clock) if guard else None
            if reason is None:
                tid, target = spec
                if isinstance(target, dict):
                    if op not in target:
                        reason = f"op {op!r} not valid for {event}"
                    else:
                        target = target[op]
            if reason is not None:
                self.sink.emit(
                    "transition.rejected", appointment_id=appointment_id, state=cur,
                    event=event, reason=reason, trace_id=trace_id, failure_class="F9",
                )
                raise IllegalTransition(cur, event, reason)

            now = iso(self.clock.now())
            cols = dict(updates or {})
            cols.update(state=str(target), version=row["version"] + 1, updated_at=now)
            if activity:
                cols["last_activity_at"] = now
            if target in TERMINAL and "terminal_reason" not in cols:
                cols["terminal_reason"] = cause or event
            for k, v in list(cols.items()):
                if isinstance(v, (dict, list)):
                    cols[k] = json.dumps(v)
            sets = ", ".join(f"{k}=?" for k in cols)
            c.execute(f"UPDATE appointments SET {sets} WHERE id=?", (*cols.values(), appointment_id))
            c.execute(
                "INSERT INTO transitions(appointment_id, from_state, to_state, event, cause, trace_id, ts)"
                " VALUES (?,?,?,?,?,?,?)",
                (appointment_id, cur, str(target), event, f"{tid}: {cause}".strip(": "), trace_id, now),
            )
            row.update(cols)
        self.sink.emit(
            "state.transition", appointment_id=appointment_id, from_state=str(cur),
            to_state=str(target), event=event, transition=tid, cause=cause, trace_id=trace_id,
        )
        return row

    def update(self, appointment_id: str, **cols: Any) -> None:
        """Non-state column updates (e.g. counters). State changes must go through apply()."""
        assert "state" not in cols, "use apply() to change state"
        cols = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in cols.items()}
        sets = ", ".join(f"{k}=?" for k in cols)
        with tx(self.db_path) as c:
            c.execute(f"UPDATE appointments SET {sets} WHERE id=?", (*cols.values(), appointment_id))

    def get(self, appointment_id: str) -> dict[str, Any]:
        with tx(self.db_path) as c:
            row = c.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
        if row is None:
            raise KeyError(appointment_id)
        return dict(row)
