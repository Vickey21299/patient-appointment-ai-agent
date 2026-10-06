"""Web UI server: chat with the agent, replay use cases, watch state/events/checks live.

  .venv/Scripts/python -m uvicorn server.app:create_server --factory --port 8000
  then open http://localhost:8000   (serves frontend/)

Each UI session gets its own runtime and SQLite DB (ui_sessions/<id>.db), so faults armed for
one use case never leak into another. Scenario sessions use a FakeClock so "advance time" works.
Gemini and Langfuse are only called when a message is sent.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.config import ROOT, settings
from agent.runtime import Runtime, build_llm, build_runtime
from eval.checks import TurnRecord, active_bookings, run_checks
from eval.harness import arm_faults
from eval.scenario import Scenario, load_all
from reliability.clock import Clock, FakeClock, iso
from storage.db import query

FRONTEND_DIR = ROOT / "frontend"
SESSIONS_DIR = ROOT / "ui_sessions"
BASELINE = ROOT / "eval" / "reports" / "baseline.json"
LIFECYCLE = ["COLLECTING", "SEARCHING", "OFFERED", "HELD", "CONFIRMING", "COMMITTING", "BOOKED"]


class NewSession(BaseModel):
    scenario_id: str | None = None


class Message(BaseModel):
    text: str
    request_id: str | None = None


class Advance(BaseModel):
    minutes: float = 11


@dataclass
class UISession:
    id: str
    rt: Runtime
    chat: Any
    clock: Clock
    db: str
    scenario: Scenario | None
    turns: list[TurnRecord] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    created_at: float = field(default_factory=time.time)


def _baseline_rates() -> dict[str, float | None]:
    if not BASELINE.exists():
        return {}
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    return {r["scenario_id"]: r["pass_rate"] for r in data["summary"]["scenarios"]}


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


APPT_COLS = ("id", "state", "patient_id", "patient_verified", "provider_id", "slot_start", "booking_id",
             "notice_status", "failed_calls", "terminal_reason", "hold_expires_at")
SID_OK = re.compile(r"^ui-[a-z0-9]+-[0-9a-f]{8}$")


def db_status(db: str, session_id: str, appointment_id: str) -> dict[str, Any]:
    """Everything the status panel shows, read from the session's SQLite file."""
    row = dict(query(db, "SELECT * FROM appointments WHERE id=?", (appointment_id,))[0])
    events = [{"i": r["id"], "name": r["name"], **json.loads(r["attrs_json"])}
              for r in query(db, "SELECT id, name, attrs_json FROM events ORDER BY id")]
    return {
        "session_id": session_id,
        "appointment": {k: row[k] for k in APPT_COLS},
        "lifecycle": LIFECYCLE,
        "terminal": row["state"] in ("CANCELLED", "EXPIRED", "ESCALATED"),
        "events": events,
        "bookings": [dict(r) for r in query(db, "SELECT id, kind, status, patient_id, provider_id, slot_start "
                                                "FROM bookings ORDER BY created_at")],
        "escalations": [{"reason": r["reason"], **json.loads(r["packet_json"])}
                        for r in query(db, "SELECT reason, packet_json FROM escalations")],
        "transcript": [dict(r) for r in query(db, "SELECT role, text, state, trace_id, request_id, error, ts "
                                                  "FROM chat_messages WHERE session_id=? ORDER BY id", (session_id,))],
    }


def _scenario_for(session_id: str) -> Scenario | None:
    tag = session_id.split("-")[1].upper()
    return next(iter(load_all([tag])), None) if tag != "CHAT" else None


def create_server(llm=None, sessions_dir: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Scheduling agent UI")
    sessions: dict[str, UISession] = {}
    state = {"llm": llm}
    sdir = Path(sessions_dir or SESSIONS_DIR)
    sdir.mkdir(parents=True, exist_ok=True)

    def get_llm():
        if state["llm"] is None:
            state["llm"] = build_llm()
        return state["llm"]

    def get(sid: str) -> UISession:
        if sid not in sessions:
            raise HTTPException(404, "unknown session")
        return sessions[sid]

    def trace_url(s: UISession, trace_id: str | None) -> str | None:
        if not (trace_id and s.rt.langfuse):
            return None
        try:
            return s.rt.langfuse.get_trace_url(trace_id=trace_id)
        except Exception:
            return None

    def status(s: UISession) -> dict[str, Any]:
        last_trace = trace_url(s, s.chat.last_trace_id)
        st = db_status(s.db, s.id, s.chat.appointment_id)
        st.update({
            "scenario_id": s.scenario.id if s.scenario else None,
            "now": iso(s.clock.now()),
            "fake_clock": isinstance(s.clock, FakeClock),
            "read_only": False,
            "faults": [{"tool": f.tool, "fault": f.fault, "calls": f.calls} for f in s.rt.injector.faults],
            "tool_calls": dict(s.rt.injector.counts),
            "langfuse": {"enabled": s.rt.langfuse is not None, "last_trace_url": last_trace,
                         "session_url": last_trace.split("/traces/")[0] + f"/sessions/{s.id}" if last_trace else None,
                         "trace_count": sum(1 for m in st["transcript"] if m["trace_id"])},
        })
        return _jsonable(st)

    @app.get("/api/meta")
    def meta():
        import os
        return {"model": settings.model,
                "tracing_enabled": os.getenv("LANGFUSE_TRACING_ENABLED", "true").lower() != "false"
                and bool(os.getenv("LANGFUSE_PUBLIC_KEY")),
                "environment": os.getenv("LANGFUSE_TRACING_ENVIRONMENT", "development")}

    @app.get("/api/scenarios")
    def scenarios():
        rates = _baseline_rates()
        return [{"id": sc.id, "title": sc.title, "failure_class": sc.primary_failure_class,
                 "why": sc.why_it_matters, "baseline_pass_rate": rates.get(sc.id),
                 "faults": sc.faults, "setup": sc.setup, "expect": sc.expect,
                 "turns": [asdict(t) for t in sc.turns]} for sc in load_all()]

    @app.post("/api/sessions")
    def new_session(body: NewSession):
        sc = None
        if body.scenario_id:
            matches = load_all([body.scenario_id])
            if not matches:
                raise HTTPException(404, "unknown scenario")
            sc = matches[0]
        sid = f"ui-{(sc.id if sc else 'chat').lower()}-{uuid.uuid4().hex[:8]}"
        db = str(sdir / f"{sid}.db")
        clock: Clock = FakeClock() if sc else Clock()
        rt = build_runtime(db, clock=clock, llm=get_llm())
        appointment_id = patient_id = None
        if sc and (pb := sc.setup.get("prebook")):
            appointment_id, patient_id = rt.prebook(tuple(pb["patient"]), pb.get("provider_id", "dr_rao"),
                                                    pb.get("slot_index", 0))
        if sc:
            arm_faults(rt, sc.faults)
        chat = rt.new_session(tags=["ui", f"scenario:{sc.id}" if sc else "free-chat"], session_id=sid,
                              appointment_id=appointment_id, patient_id=patient_id)
        s = UISession(sid, rt, chat, clock, db, sc)
        sessions[sid] = s
        return {"session_id": sid, "status": status(s)}

    @app.post("/api/sessions/{sid}/messages")
    def send(sid: str, body: Message):
        s = get(sid)
        if not body.text.strip():
            raise HTTPException(400, "empty message")
        with s.lock:
            n_before = len(s.rt.events.events)
            started = time.perf_counter()
            reply = s.chat.handle(body.text, request_id=body.request_id)
            row = s.rt.svc.sm.get(s.chat.appointment_id)
            s.turns.append(TurnRecord(body.text, reply, row["state"], row["notice_status"], active_bookings(s.db),
                                      s.chat.last_trace_id, s.chat.last_error, s.rt.events.events[n_before:]))
            return {"reply": reply, "error": s.chat.last_error, "latency_ms": int((time.perf_counter() - started) * 1000),
                    "state": row["state"], "trace_url": trace_url(s, s.chat.last_trace_id), "status": status(s)}

    @app.get("/api/sessions/{sid}/status")
    def get_status(sid: str):
        return status(get(sid))

    @app.post("/api/sessions/{sid}/advance")
    def advance(sid: str, body: Advance):
        s = get(sid)
        if not isinstance(s.clock, FakeClock):
            raise HTTPException(400, "time travel is only available in use-case sessions")
        with s.lock:
            s.clock.advance(minutes=body.minutes)
            s.rt.svc.tick()
        return status(s)

    @app.post("/api/sessions/{sid}/checks")
    def checks(sid: str):
        s = get(sid)
        row = s.rt.svc.sm.get(s.chat.appointment_id)
        groups = run_checks(s.scenario.expect if s.scenario else {}, db=s.db, injector=s.rt.injector,
                            events=s.rt.events.events, turns=s.turns, final_state=row["state"],
                            notice_status=row["notice_status"])
        return {g: [asdict(c) for c in cs] for g, cs in groups.items()}

    @app.get("/api/history")
    def history():
        """Past sessions, from the SQLite files on disk (survives server restarts)."""
        out = []
        for f in sorted(sdir.glob("ui-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
            try:
                msgs = query(str(f), "SELECT role, state, ts FROM chat_messages ORDER BY id")
            except Exception:
                continue
            if not msgs:
                continue
            sc = _scenario_for(f.stem)
            out.append({"session_id": f.stem, "scenario_id": sc.id if sc else None,
                        "title": sc.title if sc else "Free chat", "messages": len(msgs),
                        "final_state": msgs[-1]["state"], "started": msgs[0]["ts"], "last": msgs[-1]["ts"],
                        "live": f.stem in sessions})
        return out

    @app.get("/api/history/{sid}")
    def history_detail(sid: str):
        if not SID_OK.match(sid):
            raise HTTPException(400, "bad session id")
        db = sdir / f"{sid}.db"
        if not db.exists():
            raise HTTPException(404, "unknown session")
        last = query(str(db), "SELECT appointment_id FROM chat_messages WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,))
        if not last:
            raise HTTPException(404, "session has no messages")
        st = db_status(str(db), sid, last[0]["appointment_id"])
        sc = _scenario_for(sid)
        st.update({"scenario_id": sc.id if sc else None, "now": None, "fake_clock": False, "read_only": True,
                   "faults": [{"tool": f["tool"], "fault": f["fault"], "calls": f.get("calls", "all")} for f in (sc.faults if sc else [])],
                   "tool_calls": {}, "langfuse": {"enabled": False, "last_trace_url": None, "session_url": None,
                                                  "trace_count": sum(1 for m in st["transcript"] if m["trace_id"])}})
        return _jsonable(st)

    # Static frontend (index.html, app.js, api.js, styles.css). Mounted last so /api/* wins.
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app
