"""LangGraph tools. Thin wrappers over SchedulingService, bound to one chat session.

The LLM never sees or chooses the appointment ID; the session holds it. Every tool returns the
classified Outcome as JSON, so the model reasons over `kind` and `state` (I3), never raw errors.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Callable

from langchain_core.tools import tool

from reliability.service import Outcome
from reliability.state_machine import IllegalTransition

if TYPE_CHECKING:
    from agent.session import ChatSession

log = logging.getLogger("agent.tools")
MAX_SLOTS_SHOWN = 6


def _serialize(out: Outcome) -> str:
    data = dict(out.data)
    for key in ("slots", "alternatives", "offered"):
        if key in data:
            data[key] = [{k: s[k] for k in ("provider_id", "slot_start", "slot_end")} for s in data[key][:MAX_SLOTS_SHOWN]]
    data.pop("patient_id", None)
    return json.dumps({"kind": out.kind, "state": out.state, **data})


def build_tools(session: "ChatSession") -> list:
    svc = session.svc

    def run(name: str, fn: Callable[[], Outcome]) -> str:
        try:
            out = fn()
            if out.kind == "VERIFIED":
                session.patient_id = out.data.get("patient_id")
            log.info("session=%s tool=%s -> kind=%s state=%s", session.session_id, name, out.kind, out.state)
            return _serialize(out)
        except IllegalTransition as e:  # F9: tell the model why, let it re-plan
            log.warning("session=%s tool=%s rejected: %s", session.session_id, name, e)
            return json.dumps({"kind": "REJECTED", "reason": str(e), "state": svc.sm.get(session.appointment_id)["state"]})
        except Exception as e:
            log.exception("session=%s tool=%s crashed", session.session_id, name)
            return json.dumps({"kind": "ERROR", "reason": type(e).__name__})

    @tool
    def verify_patient(full_name: str, date_of_birth: str) -> str:
        """Verify the patient's identity. date_of_birth must be YYYY-MM-DD. Required before anything else."""
        def _verify() -> Outcome:
            if svc.sm.get(session.appointment_id)["state"] == "COLLECTING":
                return svc.verify_patient(session.appointment_id, full_name, date_of_birth)
            # Later in the flow (e.g. a returning patient with a booking): read-only identity check.
            return svc.check_identity(session.appointment_id, full_name, date_of_birth, session.patient_id)
        return run("verify_patient", _verify)

    @tool
    def set_visit_details(visit_type: str = "checkup", provider_id: str | None = None,
                          date_from: str | None = None, date_to: str | None = None) -> str:
        """Save what the patient wants. provider_id is 'dr_rao' or 'dr_mehta' (optional).
        date_from/date_to are ISO dates (YYYY-MM-DD), both inclusive (optional). For one day, pass the same date for both."""
        return run("set_visit_details", lambda: svc.set_details(
            session.appointment_id, visit_type=visit_type, provider_id=provider_id, date_from=date_from, date_to=date_to))

    @tool
    def search_slots(provider_id: str | None = None, date_from: str | None = None, date_to: str | None = None) -> str:
        """Search available slots. Pass new preferences to change provider or dates; any held slot is released."""
        return run("search_slots", lambda: svc.search(
            session.appointment_id, provider_id=provider_id, date_from=date_from, date_to=date_to))

    @tool
    def choose_slot(provider_id: str, slot_start: str) -> str:
        """Place a temporary hold on one of the offered slots. Use exact values from search results."""
        return run("choose_slot", lambda: svc.choose_slot(session.appointment_id, provider_id, slot_start))

    @tool
    def confirm_booking(patient_confirmed: bool) -> str:
        """Book the held slot. Call with true ONLY after the patient explicitly said yes to that exact slot;
        call with false if they declined."""
        return run("confirm_booking", lambda: svc.confirm(session.appointment_id, patient_confirmed))

    @tool
    def cancel_booking() -> str:
        """Cancel the current booked appointment. Only after the patient explicitly asked to cancel."""
        return run("cancel_booking", lambda: svc.cancel(session.appointment_id))

    @tool
    def start_reschedule() -> str:
        """Begin moving the current booked appointment. The original booking stays until the new one is confirmed."""
        return run("start_reschedule", lambda: svc.request_reschedule(session.appointment_id))

    @tool
    def find_reschedule_slots(provider_id: str | None = None, date_from: str | None = None,
                              date_to: str | None = None) -> str:
        """Search slots for the reschedule."""
        return run("find_reschedule_slots", lambda: svc.reschedule_search(
            session.appointment_id, provider_id=provider_id, date_from=date_from, date_to=date_to))

    @tool
    def choose_reschedule_slot(provider_id: str, slot_start: str) -> str:
        """Hold the new slot for the reschedule."""
        return run("choose_reschedule_slot", lambda: svc.reschedule_choose(session.appointment_id, provider_id, slot_start))

    @tool
    def confirm_reschedule(patient_confirmed: bool) -> str:
        """Complete (true) or abandon (false) the reschedule after an explicit patient answer."""
        return run("confirm_reschedule", lambda: svc.reschedule_confirm(session.appointment_id, patient_confirmed))

    @tool
    def start_new_request() -> str:
        """Start a separate new appointment request for the same verified patient (after a booking is done)."""
        def _new() -> Outcome:
            session.appointment_id = svc.start(verified_patient_id=session.patient_id)
            return svc._out("NEW_REQUEST", session.appointment_id)
        return run("start_new_request", _new)

    @tool
    def switch_patient() -> str:
        """The conversation is now about a DIFFERENT person. Discards identity and any hold; verify again."""
        def _switch() -> Outcome:
            out = svc.switch_patient(session.appointment_id)
            session.appointment_id, session.patient_id = out.appointment_id, None
            return out
        return run("switch_patient", _switch)

    @tool
    def end_request() -> str:
        """The patient no longer wants to book. Releases any hold."""
        return run("end_request", lambda: svc.abandon(session.appointment_id, "patient ended request"))

    @tool
    def get_status() -> str:
        """Current appointment state and details, straight from the database."""
        def _status() -> Outcome:
            row = svc.sm.get(session.appointment_id)
            return svc._out("STATUS", session.appointment_id, provider_id=row["provider_id"],
                            slot_start=row["slot_start"], notice_status=row["notice_status"])
        return run("get_status", _status)

    return [verify_patient, set_visit_details, search_slots, choose_slot, confirm_booking, cancel_booking,
            start_reschedule, find_reschedule_slots, choose_reschedule_slot, confirm_reschedule,
            start_new_request, switch_patient, end_request, get_status]
