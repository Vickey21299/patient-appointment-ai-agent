"""DB-level invariant checks (RELIABILITY_SPEC §5). Shared by pytest and the eval harness."""
from __future__ import annotations

from dataclasses import dataclass

from storage.db import query

WRITE_TOOL_OPS = {"hold_slot": "hold", "book_appointment": "book", "cancel_appointment": "cancel"}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


def check_invariants(db: str, injector) -> list[Check]:
    def rows(sql, params=()):
        return [dict(r) for r in query(db, sql, params)]

    out: list[Check] = []
    dup = rows("SELECT provider_id, slot_start, COUNT(*) n FROM bookings WHERE status='ACTIVE' "
               "GROUP BY provider_id, slot_start HAVING n > 1")
    out.append(Check("I1-no-double-booking", not dup, str(dup) if dup else ""))

    dup_keys = rows("SELECT book_key, COUNT(*) n FROM bookings WHERE book_key IS NOT NULL GROUP BY book_key HAVING n > 1")
    out.append(Check("I2-one-booking-per-key", not dup_keys, str(dup_keys) if dup_keys else ""))

    missing = [t for t, op in WRITE_TOOL_OPS.items()
               if injector.calls_to(t) and not rows("SELECT 1 FROM intents WHERE op=?", (op,))]
    out.append(Check("I4-intent-before-write", not missing, f"no intent for {missing}" if missing else ""))

    orphan = rows(
        "SELECT b.id FROM bookings b WHERE b.kind='HOLD' AND b.status='ACTIVE' AND NOT EXISTS ("
        " SELECT 1 FROM appointments a WHERE a.state NOT IN ('CANCELLED','EXPIRED','ESCALATED')"
        " AND (a.hold_id = b.id OR a.pending_json LIKE '%' || b.id || '%'))")
    out.append(Check("I6-no-orphan-holds", not orphan, str(orphan) if orphan else ""))

    esc_without_packet = rows("SELECT id FROM appointments a WHERE state='ESCALATED' AND NOT EXISTS "
                              "(SELECT 1 FROM escalations e WHERE e.appointment_id=a.id)")
    out.append(Check("I12-escalation-has-packet", not esc_without_packet, str(esc_without_packet) if esc_without_packet else ""))
    return out
