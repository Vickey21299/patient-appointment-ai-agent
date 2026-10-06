"""Scenario tests S1-S10 (RELIABILITY_SPEC §9) against the deterministic core.

No LLM is involved: each test drives SchedulingService directly. The YAML scenario harness
(slice 2) will replay the same cases through the LangGraph agent.
"""
import pytest

from reliability.state_machine import IllegalTransition
from tests.conftest import ASHA


def states(env, appt):
    return [r["to_state"] for r in env.rows("SELECT to_state FROM transitions WHERE appointment_id=? ORDER BY id", (appt,))]


def tool_events(env, tool):
    return [e for e in env.sink.of("tool.call") if e["tool"] == tool]


# S1 ------------------------------------------------------------------ happy path
def test_S1_happy_path(env):
    a, slot = env.to_confirming()
    out = env.svc.confirm(a, yes=True)
    assert out.kind == "BOOKED" and out.state == "BOOKED"
    assert out.data["notice_status"] == "SENT"
    assert states(env, a) == ["SEARCHING", "OFFERED", "HELD", "CONFIRMING", "COMMITTING", "BOOKED"]
    assert len(env.active_bookings()) == 1
    assert len(env.injector.calls_to("book_appointment")) == 1
    assert all(e["trace_id"] == "trace-test" for e in env.sink.of("state.transition"))   # I10
    env.assert_invariants()


# S2 ------------------------------------------------------------------ slot stolen between search and hold
def test_S2_slot_stolen_offers_fresh_alternatives(env):
    a, slots = env.to_offered()
    env.fault("hold_slot", "steal_slot", calls=1)
    taken = slots[0]
    out = env.svc.choose_slot(a, taken["provider_id"], taken["slot_start"])
    assert out.kind == "CONFLICT" and out.state == "OFFERED"
    assert taken["slot_start"] not in [s["slot_start"] for s in out.data["alternatives"]]
    assert "conflict.detected" in env.sink.names()
    alt = out.data["alternatives"][0]
    assert env.svc.choose_slot(a, alt["provider_id"], alt["slot_start"]).kind == "HELD"
    assert env.svc.confirm(a, yes=True).kind == "BOOKED"
    env.assert_invariants()


# S3 ------------------------------------------------------------------ book commits, response lost (signature case)
def test_S3_book_committed_response_lost_recovers_without_retry(env):
    a, _ = env.to_confirming()
    env.fault("book_appointment", "timeout_after_commit", calls=1)
    out = env.svc.confirm(a, yes=True)
    assert out.kind == "BOOKED" and out.data["recovered"] is True
    assert len(env.injector.calls_to("book_appointment")) == 1          # I7: no blind retry
    assert len(env.active_bookings()) == 1                               # I2: no duplicate
    assert states(env, a)[-3:] == ["COMMITTING", "RECONCILING", "BOOKED"]
    resolved = env.sink.of("reconcile.resolved")
    assert resolved and resolved[0]["result"] == "applied"
    intent = env.rows("SELECT status FROM intents WHERE op='book'")[0]
    assert intent["status"] == "RESOLVED_SUCCESS"
    env.assert_invariants()


# S4 ------------------------------------------------------------------ book times out, write never happened
def test_S4_book_not_committed_retries_with_same_key_after_readback(env):
    a, _ = env.to_confirming()
    env.fault("book_appointment", "timeout_before_commit", calls=1)
    out = env.svc.confirm(a, yes=True)
    assert out.kind == "BOOKED" and out.state == "BOOKED"
    calls = tool_events(env, "book_appointment")
    assert len(calls) == 2
    assert calls[0]["idempotency_key"] == calls[1]["idempotency_key"]  # T16: same key
    # I7: the retry happens only after reconciliation proved the write absent.
    names = [(e["name"], e.get("tool")) for e in env.sink.events]
    second_book = [i for i, n in enumerate(names) if n == ("tool.call", "book_appointment")][1]
    resolved_at = names.index(("reconcile.resolved", None))
    assert resolved_at < second_book
    assert env.sink.of("reconcile.resolved")[0]["result"] == "not_applied"
    assert len(env.active_bookings()) == 1
    env.assert_invariants()


# S5 ------------------------------------------------------------------ transient read failure recovers
def test_S5_transient_search_failure_retries_with_backoff(env):
    a = env.svc.start()
    env.svc.verify_patient(a, "Asha Verma", "1990-04-12")
    env.svc.set_details(a, visit_type="checkup")
    env.fault("search_slots", "error_503", calls=[1, 2])
    out = env.svc.search(a)
    assert out.kind == "SLOTS"
    assert len(env.sink.of("retry.scheduled")) == 2                      # I11: bounded
    assert len(env.clock.slept) == 2 and env.clock.slept[0] < env.clock.slept[1]
    env.assert_invariants()


# S6 ------------------------------------------------------------------ booked, notification fails (partial success)
def test_S6_notice_failure_keeps_booking_and_reports_truthfully(env):
    a, _ = env.to_confirming()
    env.fault("send_notification", "error_503")
    out = env.svc.confirm(a, yes=True)
    assert out.kind == "BOOKED" and out.state == "BOOKED"
    assert out.data["notice_status"] == "FAILED_RETRYING"               # I3: agent must not say "sent"
    for _ in range(2):
        env.clock.advance(seconds=201)
        env.svc.tick()
    row = env.svc.sm.get(a)
    assert row["state"] == "BOOKED" and row["notice_status"] == "FAILED_FINAL"
    assert len(env.injector.calls_to("send_notification")) == 3
    env.assert_invariants()


# S7 ------------------------------------------------------------------ duplicates at tool and semantic layers
def test_S7_tool_level_replay_creates_no_duplicate(env):
    a, _ = env.to_confirming()
    hold_id = env.svc.sm.get(a)["hold_id"]
    first = env.svc.gw.book_appointment(hold_id=hold_id, key="book-dup")
    second = env.svc.gw.book_appointment(hold_id=hold_id, key="book-dup")
    assert first.status == 201 and second.status == 200 and second.body["replayed"] is True
    assert first.body["booking_id"] == second.body["booking_id"]
    assert len(env.active_bookings()) == 1


def test_S7_semantic_duplicate_returns_existing_appointment(env):
    a, slot = env.to_confirming()
    env.svc.confirm(a, yes=True)
    b, _ = env.to_offered()
    out = env.svc.choose_slot(b, slot["provider_id"], slot["slot_start"])
    assert out.kind == "ALREADY_BOOKED" and out.data["existing_appointment_id"] == a
    assert "duplicate.detected" in env.sink.names()
    assert len(env.active_bookings()) == 1
    env.assert_invariants()


# S8 ------------------------------------------------------------------ patient changes time, then identity
def test_S8_patient_changes_preference_then_identity(env):
    a, slot = env.to_confirming()
    old_hold = env.svc.sm.get(a)["hold_id"]
    out = env.svc.search(a, provider_id="dr_mehta")
    assert out.kind == "SLOTS" and out.state == "OFFERED"
    assert all(s["provider_id"] == "dr_mehta" for s in out.data["slots"])
    assert env.rows("SELECT status FROM bookings WHERE id=?", (old_hold,))[0]["status"] == "RELEASED"

    new = env.svc.switch_patient(a)
    assert env.svc.sm.get(a)["state"] == "EXPIRED"
    b = new.appointment_id
    assert new.state == "COLLECTING" and env.svc.sm.get(b)["patient_verified"] == 0
    env.svc.set_details(b, visit_type="checkup")
    with pytest.raises(IllegalTransition, match="I8"):                   # no search/write before verification
        env.svc.search(b)
    env.assert_invariants()


def test_S8_hallucinated_slot_is_rejected(env):
    a, _ = env.to_offered()
    out = env.svc.choose_slot(a, "dr_rao", "2030-01-01T09:00:00+00:00")
    assert out.kind == "REJECTED" and out.state == "OFFERED"
    assert env.sink.of("transition.rejected")[-1]["failure_class"] == "F9"
    assert not env.injector.calls_to("hold_slot")


# S9 ------------------------------------------------------------------ reschedule saga
def _booked_then_reschedule_held(env):
    a, slot = env.to_confirming()
    env.svc.confirm(a, yes=True)
    assert env.svc.request_reschedule(a).state == "MODIFYING"
    slots = env.svc.reschedule_search(a).data["slots"]
    new = next(s for s in slots if s["slot_start"] != slot["slot_start"])
    assert env.svc.reschedule_choose(a, new["provider_id"], new["slot_start"]).kind == "HELD"
    return a, slot, new


def test_S9_reschedule_happy_path_make_before_break(env):
    a, old, new = _booked_then_reschedule_held(env)
    out = env.svc.reschedule_confirm(a, yes=True)
    assert out.kind == "RESCHEDULED" and out.state == "BOOKED"
    assert out.data["slot_start"] == new["slot_start"]
    active = env.active_bookings()
    assert len(active) == 1 and active[0]["slot_start"] == new["slot_start"]
    env.assert_invariants()


def test_S9_reschedule_cancel_old_fails_escalates_duplicate_live(env):
    a, old, new = _booked_then_reschedule_held(env)
    env.fault("cancel_appointment", "error_503")
    out = env.svc.reschedule_confirm(a, yes=True)
    assert out.kind == "ESCALATED" and out.state == "ESCALATED"
    assert len(env.active_bookings()) == 2                               # new one NOT auto-cancelled
    packet = env.rows("SELECT reason, packet_json FROM escalations WHERE appointment_id=?", (a,))[0]
    assert packet["reason"] == "DUPLICATE_LIVE"
    assert '"new_booking_id"' in packet["packet_json"] and '"old_booking_id"' in packet["packet_json"]
    assert len(env.injector.calls_to("cancel_appointment")) == 3          # bounded by saga.cancel_old_retries
    env.assert_invariants()


def test_S9_reschedule_new_booking_fails_keeps_original(env):
    a, old, new = _booked_then_reschedule_held(env)
    env.fault("book_appointment", "conflict_409")
    out = env.svc.reschedule_confirm(a, yes=True)
    assert out.kind == "RESCHEDULE_ABORTED" and out.state == "BOOKED"
    assert env.svc.sm.get(a)["slot_start"] == old["slot_start"]
    assert len(env.active_bookings()) == 1
    env.assert_invariants()


# S10 ----------------------------------------------------------------- backend outage
def test_S10_outage_opens_circuit_and_escalates_honestly(env):
    a = env.svc.start()
    env.svc.verify_patient(a, "Asha Verma", "1990-04-12")
    env.svc.set_details(a, visit_type="checkup")
    env.fault("search_slots", "error_503")
    first = env.svc.search(a)
    assert first.kind == "UNAVAILABLE" and first.data["retryable"]
    second = env.svc.search(a)
    assert second.kind == "ESCALATED" and second.state == "ESCALATED"
    assert "circuit.open" in env.sink.names()
    assert len(env.injector.calls_to("search_slots")) == 5               # breaker stops the hammering
    packet = env.rows("SELECT packet_json FROM escalations WHERE appointment_id=?", (a,))
    assert packet, "I12: escalation must carry a context packet"
    env.assert_invariants()


# Timeouts (I5) ------------------------------------------------------
def test_hold_expiry_then_idle_expiry_I5(env):
    a, _ = env.to_confirming()
    env.clock.advance(minutes=11)
    env.svc.tick()
    assert env.svc.sm.get(a)["state"] == "OFFERED"                       # T09: hold lapsed
    env.clock.advance(minutes=5)
    env.svc.tick()
    assert "patient.nudge_due" in env.sink.names()
    env.clock.advance(minutes=31)
    env.svc.tick()
    assert env.svc.sm.get(a)["state"] == "EXPIRED"                       # T25
    env.assert_invariants()


def test_yes_after_hold_expired_rehold_requires_reconfirm(env):
    a, slot = env.to_confirming()
    env.clock.advance(minutes=11)
    out = env.svc.confirm(a, yes=True)
    assert out.kind == "RECONFIRM" and out.state == "CONFIRMING"
    assert not env.injector.calls_to("book_appointment")                 # never book on an expired hold
    assert env.svc.confirm(a, yes=True).kind == "BOOKED"
    env.assert_invariants()


# Cancel -------------------------------------------------------------
def test_cancel_with_lost_response_reconciles_to_cancelled(env):
    a, _ = env.to_confirming()
    env.svc.confirm(a, yes=True)
    env.fault("cancel_appointment", "timeout_after_commit", calls=1)
    out = env.svc.cancel(a)
    assert out.kind == "CANCELLED" and out.state == "CANCELLED"
    assert len(env.injector.calls_to("cancel_appointment")) == 1
    assert not env.active_bookings()
    env.assert_invariants()


def test_identity_verification_fails_three_times_escalates(env):
    a = env.svc.start()
    for _ in range(2):
        assert env.svc.verify_patient(a, "Asha Verma", "2000-01-01").kind == "NOT_VERIFIED"
    assert env.svc.verify_patient(a, "Asha Verma", "2000-01-01").kind == "ESCALATED"


# IMP-001 regression guard: date_to is an inclusive calendar date (tool contract, §2 T02/T03)
def test_single_day_search_window_is_inclusive(env):
    a = env.svc.start()
    assert env.svc.verify_patient(a, *ASHA).kind == "VERIFIED"
    day = env.rows("SELECT substr(min(slot_start), 1, 10) AS d FROM slots WHERE provider_id='dr_rao'")[0]["d"]
    env.svc.set_details(a, visit_type="checkup", provider_id="dr_rao", date_from=day, date_to=day)
    out = env.svc.search(a)
    assert out.kind == "SLOTS", out                                       # was NO_SLOTS (T03) before IMP-001
    assert out.data["slots"] and all(s["slot_start"].startswith(day) for s in out.data["slots"])
    env.assert_invariants()
