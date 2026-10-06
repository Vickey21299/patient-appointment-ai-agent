"""SchedulingService: the only API the agent uses (SYSTEM_DESIGN §2).

Every method returns an Outcome carrying the *DB-confirmed* state, so the agent can't claim
more than is true (I3). The agent never sees raw HTTP errors, only classified outcomes.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from gateway.tools import ToolGateway
from reliability.clock import Clock, iso, parse
from reliability.events import EventSink, Tracer
from reliability.executor import Exec, Executor
from reliability.idempotency import IntentLog, make_key
from reliability.policy import FailureClass as FC
from reliability.reconciler import Reconciler
from reliability.state_machine import PRE_BOOKING, TERMINAL, IllegalTransition, State, StateMachine
from storage.db import query, tx

S = State


@dataclass
class Outcome:
    kind: str                     # e.g. BOOKED, CONFLICT, UNAVAILABLE, ESCALATED ... (see methods)
    state: str                    # DB state after the call: the only thing the agent may assert
    appointment_id: str
    data: dict[str, Any] = field(default_factory=dict)


def _pending(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row.get("pending_json") or "{}")


class SchedulingService:
    def __init__(self, db_path: str, gateway: ToolGateway, policy: dict[str, Any], clock: Clock, sink: EventSink,
                 tracer: Tracer | None = None):
        self.db_path, self.gw, self.policy, self.clock, self.sink = db_path, gateway, policy, clock, sink
        self.sm = StateMachine(db_path, clock, sink)
        self.intents = IntentLog(db_path, clock)
        self.ex = Executor(db_path, policy, clock, sink, tracer=tracer)
        self.rec = Reconciler(gateway, self.ex, self.intents, policy, clock, sink)

    @property
    def trace_id(self) -> str | None:
        return self.ex.trace_id

    @trace_id.setter
    def trace_id(self, value: str | None) -> None:
        self.ex.trace_id = value

    # ------------------------------------------------------------------ helpers
    def _out(self, kind: str, appt: str, **data: Any) -> Outcome:
        return Outcome(kind, self.sm.get(appt)["state"], appt, data)

    def _apply(self, appt: str, event: str, **kw: Any) -> dict[str, Any]:
        return self.sm.apply(appt, event, trace_id=self.trace_id, **kw)

    def _touch(self, appt: str, **cols: Any) -> None:
        self.sm.update(appt, last_activity_at=iso(self.clock.now()), nudged=0, **cols)

    def _require(self, appt: str, *states: State) -> dict[str, Any]:
        row = self.sm.get(appt)
        if row["state"] not in states:
            self.sink.emit("transition.rejected", appointment_id=appt, state=row["state"],
                           event="call", reason=f"expected one of {[str(s) for s in states]}",
                           failure_class="F9", trace_id=self.trace_id)
            raise IllegalTransition(row["state"], "call", f"expected {[str(s) for s in states]}")
        return row

    def _release_hold(self, hold_id: str | None, appt: str) -> None:
        if not hold_id:
            return
        r = self.ex.call(appt, "release_hold", lambda: self.gw.release_hold(hold_id))
        if not r.ok:  # best effort; the backend TTL sweeper cleans it up (I6)
            self.sink.emit("orphan_hold", appointment_id=appt, hold_id=hold_id, trace_id=self.trace_id)

    def _escalate(self, appt: str, reason: str, event: str = "escalate", op: str | None = None,
                  **extra: Any) -> Outcome:
        """I12: every escalation stores a complete context packet before the transition."""
        row = self.sm.get(appt)
        if row["state"] in TERMINAL:
            return self._out("ESCALATED" if row["state"] == S.ESCALATED else row["state"], appt)
        if row["state"] in PRE_BOOKING:
            self._release_hold(row["hold_id"], appt)
        packet = {
            "appointment_id": appt,
            "reason": reason,
            "state_before": row["state"],
            "patient_id": row["patient_id"],
            "slot": {"provider_id": row["provider_id"], "slot_start": row["slot_start"]},
            "booking_id": row["booking_id"],
            "intents": [dict(r) for r in query(self.db_path,
                        "SELECT op, idempotency_key, status, attempts FROM intents WHERE appointment_id=?", (appt,))],
            "transitions": [dict(r) for r in query(self.db_path,
                            "SELECT from_state, to_state, event, cause FROM transitions WHERE appointment_id=? ORDER BY id",
                            (appt,))],
            "failed_calls": row["failed_calls"],
            "suggested_next_step": extra.pop("suggested_next_step", "Review intents and contact the patient."),
            **extra,
        }
        with tx(self.db_path) as c:
            c.execute("INSERT INTO escalations VALUES (?,?,?,?,?)",
                      (f"esc_{uuid.uuid4().hex[:10]}", appt, reason, json.dumps(packet), iso(self.clock.now())))
        self.sink.emit("escalation.packet", appointment_id=appt, reason=reason, trace_id=self.trace_id)
        self._apply(appt, event, cause=reason, op=op, updates={"hold_id": None, "hold_expires_at": None})
        return self._out("ESCALATED", appt, reason=reason)

    def _failure_outcome(self, appt: str, r: Exec, what: str) -> Outcome | None:
        """Common exits: dependency down (F10) or budget exhausted (F6) both escalate."""
        if r.budget_exhausted:
            return self._escalate(appt, "F6: failure budget exhausted")
        if r.cls is FC.F10_DEPENDENCY_DOWN:
            return self._escalate(appt, f"F10: {what} unavailable (circuit open)",
                                  suggested_next_step="Backend degraded; call the patient back.")
        return None

    # ------------------------------------------------------------------ intake
    def start(self, verified_patient_id: str | None = None) -> str:
        """New appointment. `verified_patient_id` carries over identity verified earlier in the SAME
        session (a second booking). Never pass it across sessions or after switch_patient (§4.5)."""
        appt = self.sm.create(trace_id=self.trace_id)
        if verified_patient_id:
            self.sm.update(appt, patient_id=verified_patient_id, patient_verified=1)
        return appt

    def verify_patient(self, appt: str, name: str, dob: str) -> Outcome:
        row = self._require(appt, S.COLLECTING)
        r = self.ex.call(appt, "verify_patient", lambda: self.gw.verify_patient(name, dob))
        if r.ok:
            self._touch(appt, patient_id=r.result.body["patient_id"], patient_verified=1)
            return self._out("VERIFIED", appt, patient_id=r.result.body["patient_id"])
        if (esc := self._failure_outcome(appt, r, "patient service")):
            return esc
        if r.cls is FC.F1_TRANSIENT_READ:
            return self._out("UNAVAILABLE", appt)
        p = _pending(row)
        p["verify_failures"] = p.get("verify_failures", 0) + 1
        self._touch(appt, pending_json=json.dumps(p))
        if p["verify_failures"] >= 3:  # §4.5
            return self._escalate(appt, "identity verification failed 3 times")
        return self._out("NOT_VERIFIED", appt, attempts_left=3 - p["verify_failures"])

    def check_identity(self, appt: str, name: str, dob: str, expected_patient_id: str | None) -> Outcome:
        """Read-only re-verification outside COLLECTING. A different person must go through switch_patient (§4.5)."""
        r = self.ex.call(appt, "verify_patient", lambda: self.gw.verify_patient(name, dob))
        if not r.ok:
            return self._out("UNAVAILABLE" if r.cls is FC.F1_TRANSIENT_READ else "NOT_VERIFIED", appt)
        pid = r.result.body["patient_id"]
        if expected_patient_id and pid != expected_patient_id:
            return self._out("DIFFERENT_PATIENT", appt, hint="call switch_patient to help a different person")
        return self._out("VERIFIED", appt, patient_id=pid)

    def set_details(self, appt: str, *, visit_type: str, provider_id: str | None = None,
                    date_from: str | None = None, date_to: str | None = None) -> Outcome:
        row = self._require(appt, S.COLLECTING)
        p = _pending(row)
        p["prefs"] = {"provider_id": provider_id, "visit_type": visit_type, "date_from": date_from, "date_to": date_to}
        self._touch(appt, visit_type=visit_type, pending_json=json.dumps(p))
        return self._out("DETAILS_SAVED", appt)

    # ------------------------------------------------------------------ discovery
    def search(self, appt: str, **pref_changes: str | None) -> Outcome:
        row = self.sm.get(appt)
        if row["state"] == S.COLLECTING:
            self._apply(appt, "fields_complete")                       # T01 (guard enforces I8)
        elif row["state"] == S.OFFERED:
            self._apply(appt, "preferences_changed" if pref_changes else "results_stale")
        elif row["state"] == S.CONFIRMING:
            self._release_hold(row["hold_id"], appt)
            self._apply(appt, "preferences_changed", updates={"hold_id": None, "hold_expires_at": None})
            self._apply(appt, "preferences_changed")
        elif row["state"] != S.SEARCHING:
            self._require(appt, S.COLLECTING, S.OFFERED, S.CONFIRMING, S.SEARCHING)
        p = _pending(self.sm.get(appt))
        prefs = {**p.get("prefs", {}), **{k: v for k, v in pref_changes.items() if v is not None}}
        p["prefs"] = prefs
        r = self.ex.call(appt, "search_slots", lambda: self.gw.search_slots(**prefs), trace_input=prefs)
        if not r.ok:
            if (esc := self._failure_outcome(appt, r, "availability service")):
                return esc
            p["search_failures"] = p.get("search_failures", 0) + 1
            self.sm.update(appt, pending_json=json.dumps(p))
            if p["search_failures"] >= 2:  # §4.1: escalate on the second exhausted search
                return self._escalate(appt, "availability search failed twice")
            return self._out("UNAVAILABLE", appt, retryable=True)
        slots = r.result.body["slots"]
        p.update(offered=slots, fetched_at=iso(self.clock.now()), search_failures=0)
        if not slots:
            self._apply(appt, "no_slots", updates={"pending_json": p})        # T03
            return self._out("NO_SLOTS", appt)
        self._apply(appt, "slots_found", updates={"pending_json": p})         # T02
        return self._out("SLOTS", appt, slots=slots)

    def choose_slot(self, appt: str, provider_id: str, slot_start: str, allow_overlap: bool = False) -> Outcome:
        row = self._require(appt, S.OFFERED)
        p = _pending(row)
        # F7 semantic duplicate / §4.2 overlap: compare with this patient's live bookings.
        live = query(self.db_path, "SELECT id, provider_id, slot_start FROM appointments "
                     "WHERE patient_id=? AND state IN ('BOOKED','MODIFYING') AND id<>?", (row["patient_id"], appt))
        for other in live:
            if other["slot_start"] == slot_start and other["provider_id"] == provider_id:
                self.sink.emit("duplicate.detected", layer="semantic", appointment_id=appt,
                               existing=other["id"], failure_class="F7", trace_id=self.trace_id)
                return self._out("ALREADY_BOOKED", appt, existing_appointment_id=other["id"])
            if other["slot_start"] == slot_start and not allow_overlap:
                self.sink.emit("conflict.detected", kind="patient_overlap", appointment_id=appt, trace_id=self.trace_id)
                return self._out("OVERLAP", appt, existing_appointment_id=other["id"])
        # T05: stale results are refreshed before holding.
        stale_s = self.policy["timeouts"]["offered_results_stale_s"]
        if (self.clock.now() - parse(p["fetched_at"])).total_seconds() > stale_s:
            out = self.search(appt)
            if out.kind != "SLOTS":
                return out
            p = _pending(self.sm.get(appt))
        slot = next((s for s in p.get("offered", [])
                     if s["slot_start"] == slot_start and s["provider_id"] == provider_id), None)
        if slot is None:  # T04 guard: the agent may only pick an offered slot (F9, hallucinated slot)
            self.sink.emit("transition.rejected", appointment_id=appt, state="OFFERED", event="slot_chosen",
                           reason="slot not in offered list", failure_class="F9", trace_id=self.trace_id)
            return self._out("REJECTED", appt, reason="slot_not_offered", offered=p.get("offered", []))
        p["hold_seq"] = p.get("hold_seq", 0) + 1
        self._apply(appt, "slot_chosen", updates={            # T04
            "provider_id": provider_id, "slot_start": slot_start, "slot_end": slot["slot_end"], "pending_json": p})
        return self._acquire_hold(appt, p)

    def _acquire_hold(self, appt: str, p: dict[str, Any]) -> Outcome:
        row = self.sm.get(appt)
        args = {"patient_id": row["patient_id"], "provider_id": row["provider_id"],
                "slot_start": row["slot_start"], "seq": p["hold_seq"]}
        key = make_key(appt, "hold", args)
        self.intents.record(appt, "hold", key, args)                       # I4
        ttl = self.policy["timeouts"]["hold_ttl_s"]
        r = self.ex.call(appt, "hold_slot", lambda: self.gw.hold_slot(
            patient_id=row["patient_id"], provider_id=row["provider_id"], slot_start=row["slot_start"],
            ttl_seconds=ttl, key=key), idempotency_key=key, trace_input=args)
        hold = None
        if r.ok:
            hold = {"id": r.result.body["hold_id"], "expires_at": r.result.body["expires_at"]}
            self.intents.mark(key, "SUCCESS", r.result.body)
        elif r.cls is FC.F2_WRITE_UNKNOWN:
            rc = self.rec.reconcile(appt, key, "hold")                     # I7
            if rc.verdict == "applied":
                hold = {"id": rc.booking["id"], "expires_at": rc.booking["expires_at"]}
            elif rc.verdict == "unavailable":
                return self._escalate(appt, "hold outcome unknown and unreadable")
        if hold:
            self._apply(appt, "hold_ok", updates={"hold_id": hold["id"], "hold_expires_at": hold["expires_at"]})  # T06
            return self._out("HELD", appt, provider_id=row["provider_id"], slot_start=row["slot_start"],
                             hold_expires_at=hold["expires_at"])
        if (esc := self._failure_outcome(appt, r, "appointment service")):
            return esc
        if r.cls is FC.F3_CONFLICT:
            self.intents.mark(key, "CONFLICT")
            self.sink.emit("conflict.detected", kind="slot_taken", appointment_id=appt, trace_id=self.trace_id)
            self._apply(appt, "hold_conflict", cause="slot taken before hold")          # T07
            return self._conflict_with_alternatives(appt, row["provider_id"], row["slot_start"])
        if r.cls is not FC.F2_WRITE_UNKNOWN:  # F2 already resolved to RESOLVED_ABSENT by the reconciler
            self.intents.mark(key, "REJECTED")
        self._apply(appt, "hold_failed", cause=str(r.cls))
        return self._out("UNAVAILABLE", appt, retryable=True)

    def _conflict_with_alternatives(self, appt: str, provider_id: str, slot_start: str) -> Outcome:
        """§4.2: drop the taken slot, re-search, and offer the fresh alternatives."""
        out = self.search(appt)
        alts = [s for s in out.data.get("slots", [])
                if not (s["provider_id"] == provider_id and s["slot_start"] == slot_start)]
        return self._out("CONFLICT", appt, taken={"provider_id": provider_id, "slot_start": slot_start},
                         alternatives=alts)

    # ------------------------------------------------------------------ commit
    def confirm(self, appt: str, yes: bool) -> Outcome:
        row = self._require(appt, S.CONFIRMING)
        if not yes:
            self._release_hold(row["hold_id"], appt)
            self._apply(appt, "patient_no", updates={"hold_id": None, "hold_expires_at": None})   # T10
            return self._out("DECLINED", appt)
        if parse(row["hold_expires_at"]) <= self.clock.now():
            # T09: the hold lapsed. Try to re-hold the same slot; a fresh yes is required.
            self._release_hold(row["hold_id"], appt)
            self._apply(appt, "hold_expired", updates={"hold_id": None, "hold_expires_at": None})
            out = self.choose_slot(appt, row["provider_id"], row["slot_start"])
            return self._out("RECONFIRM", appt, **out.data) if out.kind == "HELD" else out
        self._apply(appt, "patient_yes")                                       # T08
        return self._commit(appt)

    def _commit(self, appt: str) -> Outcome:
        row = self.sm.get(appt)
        args = {"hold_id": row["hold_id"]}
        key = make_key(appt, "book", args)
        intent = self.intents.record(appt, "book", key, args)                 # I4
        r = self.ex.call(appt, "book_appointment",
                         lambda: self.gw.book_appointment(hold_id=row["hold_id"], key=key), idempotency_key=key,
                         trace_input=args)
        if r.ok:
            self.intents.mark(key, "SUCCESS", r.result.body)
            return self._booked(appt, "book_ok", r.result.body["booking_id"])   # T11
        if r.cls is FC.F2_WRITE_UNKNOWN:
            self._apply(appt, "book_unknown", cause=r.result.error or str(r.result.status))   # T13
            rc = self.rec.reconcile(appt, key, "book")
            if rc.verdict == "applied":
                return self._booked(appt, "readback_applied", rc.booking["id"], op="book", recovered=True)  # T15
            if rc.verdict == "unavailable":
                return self._escalate(appt, "book outcome unknown and unreadable", "readback_unavailable")  # T17
            retries_used = intent["attempts"] - 1
            if retries_used < self.policy["writes"]["max_retries_after_absent"] and not r.budget_exhausted:
                self._apply(appt, "retry_write", op="book", cause="confirmed absent, same key")  # T16
                return self._commit(appt)
            self._apply(appt, "readback_not_applied", op="book",
                        updates={"hold_id": None, "hold_expires_at": None})
            return self._out("UNAVAILABLE", appt, retryable=True)
        if (esc := self._failure_outcome(appt, r, "appointment service")):
            return esc
        if r.cls is FC.F3_CONFLICT:                                              # T12
            self.intents.mark(key, "CONFLICT")
            self.sink.emit("conflict.detected", kind=r.result.error_code or "slot_taken",
                           appointment_id=appt, trace_id=self.trace_id)
            self._apply(appt, "book_conflict", updates={"hold_id": None, "hold_expires_at": None})
            return self._conflict_with_alternatives(appt, row["provider_id"], row["slot_start"])
        self.intents.mark(key, "REJECTED")                                       # T14
        self._apply(appt, "book_invalid", updates={"hold_id": None, "hold_expires_at": None})
        return self._out("REJECTED", appt, reason=r.result.error_code)

    def _booked(self, appt: str, event: str, booking_id: str, op: str | None = None, recovered: bool = False) -> Outcome:
        self._apply(appt, event, op=op, updates={"booking_id": booking_id, "hold_id": None,
                                                 "hold_expires_at": None, "notice_status": "PENDING"})
        self._enqueue(appt, "booking_confirmation")
        self.process_outbox()
        row = self.sm.get(appt)
        return self._out("BOOKED", appt, booking_id=booking_id, provider_id=row["provider_id"],
                         slot_start=row["slot_start"], notice_status=row["notice_status"], recovered=recovered)

    # ------------------------------------------------------------------ cancel
    def cancel(self, appt: str) -> Outcome:
        row = self._require(appt, S.BOOKED)
        self._apply(appt, "cancel_request")                                      # T22
        return self._cancel_call(appt, row["booking_id"])

    def _cancel_call(self, appt: str, booking_id: str) -> Outcome:
        args = {"booking_id": booking_id}
        key = make_key(appt, "cancel", args)
        intent = self.intents.record(appt, "cancel", key, args)
        r = self.ex.call(appt, "cancel_appointment",
                         lambda: self.gw.cancel_appointment(booking_id=booking_id, key=key), idempotency_key=key,
                         trace_input=args)
        if r.ok:
            self.intents.mark(key, "SUCCESS", r.result.body)
            return self._cancelled(appt, "cancel_ok")                             # T23
        if r.cls is FC.F2_WRITE_UNKNOWN:
            self._apply(appt, "cancel_unknown", cause=r.result.error or str(r.result.status))   # T24
            rc = self.rec.reconcile(appt, key, "cancel")
            if rc.verdict == "applied":
                return self._cancelled(appt, "readback_applied", op="cancel")
            if rc.verdict == "unavailable":
                return self._escalate(appt, "cancel outcome unknown and unreadable", "readback_unavailable")
            if intent["attempts"] - 1 < self.policy["writes"]["max_retries_after_absent"] and not r.budget_exhausted:
                self._apply(appt, "retry_write", op="cancel", cause="confirmed absent, same key")
                return self._cancel_call(appt, booking_id)
            self._apply(appt, "readback_not_applied", op="cancel")
            return self._out("UNAVAILABLE", appt, retryable=True)
        if (esc := self._failure_outcome(appt, r, "appointment service")):
            return esc
        self.intents.mark(key, "REJECTED")
        self._apply(appt, "cancel_rejected", cause=r.result.error_code or "")
        return self._out("REJECTED", appt, reason=r.result.error_code)

    def _cancelled(self, appt: str, event: str, op: str | None = None) -> Outcome:
        self._apply(appt, event, op=op, updates={"notice_status": "PENDING"})
        self._enqueue(appt, "cancellation_notice")
        self.process_outbox()
        return self._out("CANCELLED", appt, notice_status=self.sm.get(appt)["notice_status"])

    # ------------------------------------------------------------------ reschedule (saga, make-before-break)
    def request_reschedule(self, appt: str) -> Outcome:
        self._require(appt, S.BOOKED)
        self._apply(appt, "reschedule_request")                                  # T18
        return self._out("MODIFYING", appt)

    def reschedule_search(self, appt: str, **prefs: str | None) -> Outcome:
        row = self._require(appt, S.MODIFYING)
        p = _pending(row)
        q = {**p.get("prefs", {}), **{k: v for k, v in prefs.items() if v is not None}}
        r = self.ex.call(appt, "search_slots", lambda: self.gw.search_slots(**q))
        if not r.ok:
            if r.budget_exhausted or r.cls is FC.F10_DEPENDENCY_DOWN:
                return self._abort_reschedule(appt, "availability unavailable")
            return self._out("UNAVAILABLE", appt, retryable=True)
        p["resched_offered"] = r.result.body["slots"]
        self.sm.update(appt, pending_json=json.dumps(p))
        return self._out("SLOTS", appt, slots=p["resched_offered"])

    def reschedule_choose(self, appt: str, provider_id: str, slot_start: str) -> Outcome:
        row = self._require(appt, S.MODIFYING)
        p = _pending(row)
        if not any(s["provider_id"] == provider_id and s["slot_start"] == slot_start for s in p.get("resched_offered", [])):
            return self._out("REJECTED", appt, reason="slot_not_offered")
        p["hold_seq"] = p.get("hold_seq", 0) + 1
        args = {"patient_id": row["patient_id"], "provider_id": provider_id, "slot_start": slot_start, "seq": p["hold_seq"]}
        key = make_key(appt, "hold", args)
        self.intents.record(appt, "hold", key, args)
        r = self.ex.call(appt, "hold_slot", lambda: self.gw.hold_slot(
            patient_id=row["patient_id"], provider_id=provider_id, slot_start=slot_start,
            ttl_seconds=self.policy["timeouts"]["hold_ttl_s"], key=key), idempotency_key=key)
        hold = r.result.body.get("hold_id") if r.ok else None
        if r.cls is FC.F2_WRITE_UNKNOWN:
            rc = self.rec.reconcile(appt, key, "hold")
            hold = rc.booking["id"] if rc.verdict == "applied" else None
        if not hold:
            self.sm.update(appt, pending_json=json.dumps(p))
            if r.cls is FC.F3_CONFLICT:
                return self._out("CONFLICT", appt, taken={"provider_id": provider_id, "slot_start": slot_start})
            return self._out("UNAVAILABLE", appt, retryable=True)
        self.intents.mark(key, "SUCCESS")
        p["new"] = {"hold_id": hold, "provider_id": provider_id, "slot_start": slot_start,
                    "slot_end": next(s["slot_end"] for s in p["resched_offered"] if s["slot_start"] == slot_start
                                     and s["provider_id"] == provider_id)}
        self.sm.update(appt, pending_json=json.dumps(p))
        self.sink.emit("saga.step", appointment_id=appt, step="hold_new", status="ok", trace_id=self.trace_id)
        return self._out("HELD", appt, provider_id=provider_id, slot_start=slot_start)

    def reschedule_confirm(self, appt: str, yes: bool) -> Outcome:
        row = self._require(appt, S.MODIFYING)
        p = _pending(row)
        new = p.get("new")
        if not new:
            return self._out("REJECTED", appt, reason="no_new_slot_held")
        if not yes:
            return self._abort_reschedule(appt, "patient declined new slot")
        # Saga step 2: book the new slot. The old booking stays live (make-before-break).
        key = make_key(appt, "book", {"hold_id": new["hold_id"]})
        self.intents.record(appt, "book", key, {"hold_id": new["hold_id"]})
        r = self.ex.call(appt, "book_appointment",
                         lambda: self.gw.book_appointment(hold_id=new["hold_id"], key=key), idempotency_key=key)
        new_booking = r.result.body.get("booking_id") if r.ok else None
        if r.cls is FC.F2_WRITE_UNKNOWN:
            rc = self.rec.reconcile(appt, key, "book")
            if rc.verdict == "unavailable":
                return self._escalate(appt, "reschedule: new booking outcome unknown", "reschedule_stuck",
                                      new_hold_id=new["hold_id"])
            new_booking = rc.booking["id"] if rc.verdict == "applied" else None
        if not new_booking:
            return self._abort_reschedule(appt, f"new booking failed ({r.cls})")   # T20
        self.intents.mark(key, "SUCCESS")
        self.sink.emit("saga.step", appointment_id=appt, step="book_new", status="ok", trace_id=self.trace_id)
        # Saga step 3: cancel the old booking, bounded retries, reconciling unknowns first (I7).
        old = row["booking_id"]
        ckey = make_key(appt, "cancel", {"booking_id": old})
        for _ in range(self.policy["saga"]["cancel_old_retries"]):
            self.intents.record(appt, "cancel", ckey, {"booking_id": old})
            c = self.ex.call(appt, "cancel_appointment",
                             lambda: self.gw.cancel_appointment(booking_id=old, key=ckey), idempotency_key=ckey)
            done = c.ok
            if c.cls is FC.F2_WRITE_UNKNOWN:
                done = self.rec.reconcile(appt, ckey, "cancel").verdict == "applied"
            if done:
                self.intents.mark(ckey, "SUCCESS")
                self.sink.emit("saga.step", appointment_id=appt, step="cancel_old", status="ok", trace_id=self.trace_id)
                p.pop("new", None)
                self._apply(appt, "reschedule_done", updates={                     # T19
                    "booking_id": new_booking, "provider_id": new["provider_id"], "slot_start": new["slot_start"],
                    "slot_end": new["slot_end"], "notice_status": "PENDING", "pending_json": p})
                self._enqueue(appt, "reschedule_confirmation")
                self.process_outbox()
                row = self.sm.get(appt)
                return self._out("RESCHEDULED", appt, booking_id=new_booking, provider_id=row["provider_id"],
                                 slot_start=row["slot_start"], notice_status=row["notice_status"])
        # T21: both bookings live. Never auto-cancel the new one (§4.3); hand to a human.
        self.sink.emit("saga.step", appointment_id=appt, step="cancel_old", status="failed", trace_id=self.trace_id)
        self.sink.emit("compensation.run", appointment_id=appt, action="escalate_duplicate_live", trace_id=self.trace_id)
        return self._escalate(appt, "DUPLICATE_LIVE", "reschedule_stuck", old_booking_id=old,
                              new_booking_id=new_booking, flag="DUPLICATE_LIVE",
                              suggested_next_step="Cancel the old booking manually after confirming with the patient.")

    def _abort_reschedule(self, appt: str, reason: str) -> Outcome:
        row = self.sm.get(appt)
        p = _pending(row)
        if (new := p.pop("new", None)):
            self._release_hold(new["hold_id"], appt)
        self._apply(appt, "reschedule_aborted", cause=reason, updates={"pending_json": p})   # T20
        return self._out("RESCHEDULE_ABORTED", appt, reason=reason, original_slot=row["slot_start"])

    # ------------------------------------------------------------------ patient changes / timeouts
    def abandon(self, appt: str, reason: str = "patient abandoned") -> Outcome:
        row = self.sm.get(appt)
        if row["state"] not in PRE_BOOKING:
            return self._out("REJECTED", appt, reason=f"cannot abandon in {row['state']}")
        self._release_hold(row["hold_id"], appt)
        self._apply(appt, "patient_abandon", cause=reason, updates={"hold_id": None, "hold_expires_at": None})
        return self._out("EXPIRED", appt)

    def switch_patient(self, appt: str) -> Outcome:
        """§4.5: a different patient. Never reuse identity, holds or collected details."""
        self.sink.emit("patient.change", kind="identity_switch", appointment_id=appt, trace_id=self.trace_id)
        row = self.sm.get(appt)
        if row["state"] in PRE_BOOKING:
            self.abandon(appt, "identity switch")
        new = self.start()
        return self._out("NEW_SESSION", new, previous_appointment_id=appt)

    def tick(self) -> None:
        """Timeouts (T09, T25, I5) and the outbox. The harness calls this after advancing the clock."""
        now = self.clock.now()
        t = self.policy["timeouts"]
        for row in query(self.db_path, "SELECT * FROM appointments WHERE state IN (%s)"
                         % ",".join(f"'{s}'" for s in PRE_BOOKING)):
            row, appt = dict(row), row["id"]
            idle = (now - parse(row["last_activity_at"])).total_seconds()
            if idle >= t["idle_timeout_s"]:
                self._release_hold(row["hold_id"], appt)
                self._apply(appt, "idle_timeout", cause=f"idle {int(idle)}s", activity=False,
                            updates={"hold_id": None, "hold_expires_at": None})
                continue
            if row["state"] == S.CONFIRMING and parse(row["hold_expires_at"]) <= now:
                self._release_hold(row["hold_id"], appt)  # I6: release explicitly, don't rely on the TTL sweeper
                self._apply(appt, "hold_expired", activity=False, updates={"hold_id": None, "hold_expires_at": None})
            if idle >= t["idle_timeout_s"] * t["nudge_at_fraction"] and not row["nudged"]:
                self.sm.update(appt, nudged=1)
                self.sink.emit("patient.nudge_due", appointment_id=appt, trace_id=self.trace_id)
        self.process_outbox()

    # ------------------------------------------------------------------ outbox (best-effort side effects, §4.3)
    def _enqueue(self, appt: str, kind: str) -> None:
        with tx(self.db_path) as c:
            c.execute("INSERT INTO outbox VALUES (?,?,?, 'PENDING', 0, ?)",
                      (f"ob_{uuid.uuid4().hex[:10]}", appt, kind, iso(self.clock.now())))

    def process_outbox(self) -> None:
        now = self.clock.now()
        cfg = self.policy["outbox"]
        due = query(self.db_path, "SELECT o.*, a.booking_id FROM outbox o JOIN appointments a ON a.id=o.appointment_id "
                    "WHERE o.status IN ('PENDING','FAILED_RETRYING') AND o.next_attempt_at <= ?", (iso(now),))
        for o in due:
            # Same key on every attempt: the endpoint dedupes, so a resend is a replay, not a blind retry (I7).
            r = self.ex.call(None, "send_notification", lambda: self.gw.send_notification(
                booking_id=o["booking_id"], kind=o["kind"], key=f"notify-{o['id']}"), idempotency_key=f"notify-{o['id']}",
                trace_input={"kind": o["kind"], "attempt": o["attempts"] + 1})
            attempts = o["attempts"] + 1
            if r.ok:
                status = "SENT"
            elif attempts >= cfg["max_attempts"]:
                status = "FAILED_FINAL"
            else:
                status = "FAILED_RETRYING"
            nxt = iso(now + timedelta(seconds=cfg["retry_spacing_s"]))
            with tx(self.db_path) as c:
                c.execute("UPDATE outbox SET status=?, attempts=?, next_attempt_at=? WHERE id=?",
                          (status, attempts, nxt, o["id"]))
                c.execute("UPDATE appointments SET notice_status=? WHERE id=?", (status, o["appointment_id"]))
            self.sink.emit("saga.step", appointment_id=o["appointment_id"], step=o["kind"], status=status,
                           attempt=attempts, failure_class=None if r.ok else "F5", trace_id=self.trace_id)
