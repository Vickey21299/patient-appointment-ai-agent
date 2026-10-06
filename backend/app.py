"""Mock clinical backend: Patient, Availability and Appointment services (SYSTEM_DESIGN §2).

It knows nothing about the agent. It enforces its own invariants: the unique active slot (I1)
and idempotent replay by key (I2).
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from reliability.clock import Clock, iso, parse
from storage.db import query, tx


class VerifyIn(BaseModel):
    name: str
    dob: str


class HoldIn(BaseModel):
    patient_id: str
    provider_id: str
    slot_start: str
    idempotency_key: str
    ttl_seconds: int = 600


class BookIn(BaseModel):
    hold_id: str
    idempotency_key: str


class CancelIn(BaseModel):
    idempotency_key: str


class NotifyIn(BaseModel):
    booking_id: str
    kind: str
    idempotency_key: str


def _sweep(c: sqlite3.Connection, now: str) -> None:
    c.execute(
        "UPDATE bookings SET status='EXPIRED', updated_at=? "
        "WHERE kind='HOLD' AND status='ACTIVE' AND expires_at <= ?",
        (now, now),
    )


def _booking(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in ("id", "kind", "status", "patient_id", "provider_id", "slot_start", "expires_at")}


def create_app(db_path: str, clock: Clock) -> FastAPI:
    app = FastAPI(title="Mock clinical backend")

    def now() -> str:
        return iso(clock.now())

    @app.post("/patients/verify")
    def verify(body: VerifyIn):
        rows = query(db_path, "SELECT id FROM patients WHERE lower(name)=lower(?) AND dob=?", (body.name.strip(), body.dob))
        if not rows:
            raise HTTPException(404, {"error": "patient_not_found"})
        return {"patient_id": rows[0]["id"]}

    @app.get("/slots")
    def slots(provider_id: str | None = None, visit_type: str | None = None,
              date_from: str | None = None, date_to: str | None = None):
        t = now()
        with tx(db_path) as c:
            _sweep(c, t)
            sql = ("SELECT s.* FROM slots s WHERE s.slot_start > ? AND NOT EXISTS ("
                   " SELECT 1 FROM bookings b WHERE b.provider_id=s.provider_id"
                   " AND b.slot_start=s.slot_start AND b.status='ACTIVE')")
            params: list = [t]
            for col, val, op in (("provider_id", provider_id, "="), ("visit_type", visit_type, "="),
                                 ("slot_start", date_from, ">="), ("slot_start", date_to, "<")):
                if val:
                    sql += f" AND s.{col} {op} ?"
                    params.append(val)
            rows = c.execute(sql + " ORDER BY s.slot_start, s.provider_id LIMIT 20", params).fetchall()
        return {"slots": [dict(r) for r in rows]}

    @app.post("/holds", status_code=201)
    def hold(body: HoldIn):
        t = now()
        with tx(db_path) as c:
            _sweep(c, t)
            prior = c.execute("SELECT * FROM bookings WHERE idempotency_key=?", (body.idempotency_key,)).fetchone()
            if prior:  # I2: replay
                return JSONResponse({**_booking(prior), "hold_id": prior["id"], "replayed": True}, 200)
            if not c.execute("SELECT 1 FROM slots WHERE provider_id=? AND slot_start=?",
                             (body.provider_id, body.slot_start)).fetchone():
                raise HTTPException(400, {"error": "unknown_slot"})
            hid = f"bk_{uuid.uuid4().hex[:10]}"
            exp = iso(parse(t) + timedelta(seconds=body.ttl_seconds))
            try:
                c.execute(
                    "INSERT INTO bookings(id, kind, status, patient_id, provider_id, slot_start,"
                    " idempotency_key, expires_at, created_at, updated_at)"
                    " VALUES (?, 'HOLD', 'ACTIVE', ?, ?, ?, ?, ?, ?, ?)",
                    (hid, body.patient_id, body.provider_id, body.slot_start, body.idempotency_key, exp, t, t),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, {"error": "slot_taken"})  # I1
        return {"hold_id": hid, "expires_at": exp, "replayed": False}

    @app.delete("/holds/{hold_id}")
    def release(hold_id: str):
        with tx(db_path) as c:
            c.execute("UPDATE bookings SET status='RELEASED', updated_at=? "
                      "WHERE id=? AND kind='HOLD' AND status='ACTIVE'", (now(), hold_id))
        return {"released": True}

    @app.post("/appointments", status_code=201)
    def book(body: BookIn):
        t = now()
        with tx(db_path) as c:
            _sweep(c, t)
            prior = c.execute("SELECT * FROM bookings WHERE book_key=?", (body.idempotency_key,)).fetchone()
            if prior:  # I2: replay
                return JSONResponse({**_booking(prior), "booking_id": prior["id"], "replayed": True}, 200)
            h = c.execute("SELECT * FROM bookings WHERE id=?", (body.hold_id,)).fetchone()
            if h is None:
                raise HTTPException(400, {"error": "unknown_hold"})
            if h["kind"] != "HOLD" or h["status"] != "ACTIVE":
                raise HTTPException(409, {"error": "hold_expired" if h["status"] == "EXPIRED" else "hold_unavailable"})
            c.execute("UPDATE bookings SET kind='BOOKING', book_key=?, expires_at=NULL, updated_at=? WHERE id=?",
                      (body.idempotency_key, t, h["id"]))
        return {"booking_id": h["id"], "provider_id": h["provider_id"], "slot_start": h["slot_start"], "replayed": False}

    @app.post("/appointments/{booking_id}/cancel")
    def cancel(booking_id: str, body: CancelIn):
        with tx(db_path) as c:
            b = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
            if b is None:
                raise HTTPException(404, {"error": "unknown_booking"})
            if b["cancel_key"] == body.idempotency_key:
                return {"booking_id": booking_id, "status": "CANCELLED", "replayed": True}
            if b["kind"] != "BOOKING" or b["status"] != "ACTIVE":
                raise HTTPException(409, {"error": "not_cancellable", "status": b["status"]})
            c.execute("UPDATE bookings SET status='CANCELLED', cancel_key=?, updated_at=? WHERE id=?",
                      (body.idempotency_key, now(), booking_id))
        return {"booking_id": booking_id, "status": "CANCELLED", "replayed": False}

    @app.get("/bookings/by-key/{key}")
    def by_key(key: str):
        """Read-back for reconciliation (T15-T17): did the write carrying this key take effect?"""
        rows = query(db_path, "SELECT * FROM bookings WHERE idempotency_key=? OR book_key=? OR cancel_key=?",
                     (key, key, key))
        if not rows:
            return {"found": False}
        r = rows[0]
        op = "cancel" if r["cancel_key"] == key else "book" if r["book_key"] == key else "hold"
        return {"found": True, "op": op, "booking": _booking(r)}

    @app.get("/appointments/{booking_id}")
    def get_booking(booking_id: str):
        rows = query(db_path, "SELECT * FROM bookings WHERE id=?", (booking_id,))
        if not rows:
            raise HTTPException(404, {"error": "unknown_booking"})
        return _booking(rows[0])

    @app.post("/notifications", status_code=202)
    def notify(body: NotifyIn):
        with tx(db_path) as c:
            if not c.execute("SELECT 1 FROM notifications WHERE idempotency_key=?", (body.idempotency_key,)).fetchone():
                c.execute("INSERT INTO notifications VALUES (?,?,?,?,?)",
                          (f"nt_{uuid.uuid4().hex[:10]}", body.booking_id, body.kind, body.idempotency_key, now()))
        return {"queued": True}

    return app
