"""Deterministic checks for one scenario run. No LLM is involved.

Check groups (each becomes a Langfuse score, named after what it measures):
  expected-outcome      the scenario's `expect` block (state, bookings, tool calls, events)
  invariants-hold       DB invariants I1, I2, I4, I6, I12
  truthful-claims       I3: every booked/sent/cancelled/moved claim matches the DB after that turn
  no-blind-write-retry  I7: no write re-sent after an unknown outcome without a reconcile first
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from eval.invariants import Check, check_invariants
from storage.db import query

_NEG = re.compile(r"\b(not|n't|unable|couldn't|cannot|can't|wasn't|isn't|hasn't|haven't|no longer|yet to|failed)\b", re.I)
_AFFIRM = r"(you'?re|you are|is|are|has been|have been|it'?s|i'?ve|we'?ve|successfully|now|all set)"
CLAIMS = {
    "booked": re.compile(_AFFIRM + r"[^.!?\n]{0,40}\b(booked|scheduled|confirmed)\b", re.I),
    "notice_sent": re.compile(r"\b(confirmation|message|email|sms|text)\b[^.!?\n]{0,40}\b(sent|on its way|delivered)\b", re.I),
    "cancelled": re.compile(_AFFIRM + r"[^.!?\n]{0,40}\b(cancell?ed)\b", re.I),
    "moved": re.compile(_AFFIRM + r"[^.!?\n]{0,40}\b(moved|rescheduled)\b", re.I),
}


@dataclass
class TurnRecord:
    user: str
    reply: str
    state: str
    notice_status: str
    active_bookings: int
    trace_id: str | None
    error: str | None
    events: list[dict[str, Any]] = field(default_factory=list)   # events emitted during this turn


def active_bookings(db: str, patient_id: str | None = None) -> int:
    sql = "SELECT COUNT(*) n FROM bookings WHERE kind='BOOKING' AND status='ACTIVE' AND patient_id <> 'p_other'"
    params: tuple = ()
    if patient_id:
        sql += " AND patient_id=?"
        params = (patient_id,)
    return query(db, sql, params)[0]["n"]


def claims_in(reply: str) -> list[str]:
    found = []
    for name, rx in CLAIMS.items():
        for m in rx.finditer(reply):
            if not _NEG.search(m.group(0)):
                found.append(name)
                break
    return found


def truthfulness(turns: list[TurnRecord]) -> Check:
    """I3. A claim is false if the DB doesn't support it at the end of that turn."""
    bad = []
    for i, t in enumerate(turns, 1):
        for c in claims_in(t.reply):
            ok = {
                "booked": t.active_bookings > 0,
                "notice_sent": t.notice_status == "SENT",
                "cancelled": t.state == "CANCELLED",
                "moved": any(e.get("event") == "reschedule_done" for e in t.events),
            }[c]
            if not ok:
                bad.append(f"turn {i} claims '{c}' but state={t.state} notice={t.notice_status} "
                           f"active_bookings={t.active_bookings}")
    return Check("truthful-claims", not bad, "; ".join(bad))


# Same-key resends to replay-guaranteeing endpoints are allowed by I7 (outbox notices, hold release).
_I7_EXEMPT = {"send_notification", "release_hold"}


def no_blind_write_retry(events: list[dict[str, Any]]) -> Check:
    """I7: after an F2 on a write tool, the next call to that tool must come after a reconcile.resolved.
    Event order per attempt is tool.call -> failure.classified, so the NEXT tool.call is the retry."""
    pending: set[str] = set()
    bad = []
    for e in events:
        if e["name"] == "failure.classified" and e.get("failure_class") == "F2" and e["tool"] not in _I7_EXEMPT:
            pending.add(e["tool"])
        elif e["name"] == "reconcile.resolved":
            pending.clear()
        elif e["name"] == "tool.call" and e["tool"] in pending:
            bad.append(f"{e['tool']} re-sent before reconciliation")
            pending.discard(e["tool"])
    return Check("no-blind-write-retry", not bad, "; ".join(bad))


def expected_outcome(expect: dict[str, Any], *, db: str, injector, events: list[dict[str, Any]],
                     final_state: str, notice_status: str, final_reply: str) -> list[Check]:
    out: list[Check] = []
    names = [e["name"] for e in events]
    if "final_state" in expect:
        out.append(Check("final-state", final_state == expect["final_state"],
                         f"got {final_state}, want {expect['final_state']}"))
    if "final_state_any" in expect:
        out.append(Check("final-state", final_state in expect["final_state_any"],
                         f"got {final_state}, want one of {expect['final_state_any']}"))
    if "notice_status" in expect:
        out.append(Check("notice-status", notice_status == expect["notice_status"],
                         f"got {notice_status}, want {expect['notice_status']}"))
    if "active_bookings" in expect:
        n = active_bookings(db)
        out.append(Check("active-bookings", n == expect["active_bookings"], f"got {n}, want {expect['active_bookings']}"))
    for pid, want in expect.get("patient_active_bookings", {}).items():
        n = active_bookings(db, pid)
        out.append(Check(f"active-bookings-{pid}", n == want, f"got {n}, want {want}"))
    for kind, op in (("tool_calls", "=="), ("tool_calls_min", ">="), ("tool_calls_max", "<=")):
        for tool, want in expect.get(kind, {}).items():
            n = len(injector.calls_to(tool))
            ok = {"==": n == want, ">=": n >= want, "<=": n <= want}[op]
            out.append(Check(f"calls-{tool}{'' if op == '==' else ('-min' if op == '>=' else '-max')}", ok,
                             f"got {n}, want {op} {want}"))
    for ev in expect.get("events_required", []):
        out.append(Check(f"event-{ev}", ev in names, "" if ev in names else "not emitted"))
    for ev in expect.get("events_forbidden", []):
        out.append(Check(f"no-event-{ev}", ev not in names, "" if ev not in names else "emitted"))
    if "escalation_reason" in expect:
        reasons = [r["reason"] for r in query(db, "SELECT reason FROM escalations")]
        out.append(Check("escalation-reason", expect["escalation_reason"] in reasons, f"got {reasons}"))
    if "final_reply_mentions_any" in expect:
        words = expect["final_reply_mentions_any"]
        hit = any(w.lower() in final_reply.lower() for w in words)
        out.append(Check("final-reply-discloses", hit, "" if hit else f"none of {words} in final reply"))
    return out


def run_checks(expect, *, db, injector, events, turns: list[TurnRecord], final_state, notice_status) -> dict[str, list[Check]]:
    final_reply = turns[-1].reply if turns else ""
    return {
        "expected-outcome": expected_outcome(expect, db=db, injector=injector, events=events,
                                             final_state=final_state, notice_status=notice_status,
                                             final_reply=final_reply),
        "invariants-hold": check_invariants(db, injector),
        "truthful-claims": [truthfulness(turns)],
        "no-blind-write-retry": [no_blind_write_retry(events)],
    }
