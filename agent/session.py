"""One patient conversation. Each patient message = one Langfuse trace ("patient-turn");
all turns share a session_id so the whole conversation appears in the Sessions view."""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import nullcontext
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from agent.graph import build_graph
from agent.prompts import SYSTEM_PROMPT, render_context
from agent.tools import build_tools
from reliability.clock import iso
from reliability.service import SchedulingService
from storage.db import query, tx

log = logging.getLogger("agent.session")


class ChatSession:
    def __init__(self, svc: SchedulingService, llm, langfuse=None, recursion_limit: int = 25,
                 tags: list[str] | None = None, session_id: str | None = None,
                 appointment_id: str | None = None, patient_id: str | None = None):
        self.svc, self.langfuse = svc, langfuse
        self.session_id = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        self.tags = ["scheduling-agent", *(tags or [])]
        self.recursion_limit = recursion_limit
        self.patient_id: str | None = patient_id
        self.history: list[BaseMessage] = []
        # Attach to an existing appointment (e.g. a returning patient's booking) or start a new one.
        self.appointment_id = appointment_id or svc.start()
        self.graph = build_graph(llm, build_tools(self), self._system_prompt)
        self.last_trace_id: str | None = None
        self.last_error: str | None = None     # set when a turn crashed (infra error, not agent behaviour)
        self._handler = None
        if langfuse is not None:
            from langfuse.langchain import CallbackHandler
            self._handler = CallbackHandler()
        log.info("session=%s started appt=%s", self.session_id, self.appointment_id)

    def _system_prompt(self) -> str:
        row = self.svc.sm.get(self.appointment_id)
        return SYSTEM_PROMPT + render_context(iso(self.svc.clock.now())[:16], row)

    # ------------------------------------------------------------------ transport-level dedupe (F7)
    def _dedupe_get(self, request_id: str) -> str | None:
        rows = query(self.svc.db_path, "SELECT response_json FROM request_dedupe WHERE request_id=?", (request_id,))
        return json.loads(rows[0]["response_json"])["reply"] if rows else None

    def _dedupe_put(self, request_id: str, reply: str) -> None:
        with tx(self.svc.db_path) as c:
            c.execute("INSERT OR IGNORE INTO request_dedupe VALUES (?,?,?)",
                      (request_id, json.dumps({"reply": reply}), iso(self.svc.clock.now())))

    # ------------------------------------------------------------------ one turn
    def handle(self, text: str, request_id: str | None = None) -> str:
        self._save_message("user", text, request_id=request_id)
        if request_id and (cached := self._dedupe_get(request_id)) is not None:
            self.svc.sink.emit("duplicate.detected", layer="transport", request_id=request_id,
                               appointment_id=self.appointment_id, failure_class="F7")
            self._save_message("agent", cached, request_id=request_id)
            return cached

        started = time.perf_counter()
        log.info("session=%s turn start appt=%s state=%s", self.session_id, self.appointment_id,
                 self.svc.sm.get(self.appointment_id)["state"])
        lf = self.langfuse
        if lf is not None:
            from langfuse import propagate_attributes
            attrs = propagate_attributes(
                trace_name="patient-turn", session_id=self.session_id,
                user_id=self.patient_id, tags=self.tags,
                metadata={"appointment_id": self.appointment_id})
            root_cm = lf.start_as_current_observation(
                name="handle-patient-turn", as_type="agent", input=[{"role": "user", "content": text}])
        else:
            attrs, root_cm = nullcontext(), nullcontext()

        with attrs, root_cm as root:
            if lf is not None:
                self.last_trace_id = lf.get_current_trace_id()
            self.svc.trace_id = self.last_trace_id or self.session_id   # DB transitions link to the trace
            config: dict[str, Any] = {"recursion_limit": self.recursion_limit}
            if self._handler:
                config["callbacks"] = [self._handler]
            self.last_error = None
            try:
                result = self.graph.invoke({"messages": self.history + [HumanMessage(text)]}, config=config)
                self.history = result["messages"]
                reply = self.history[-1].text or "(no reply)"
                level = "DEFAULT"
            except Exception as e:
                log.exception("session=%s turn failed", self.session_id)
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                reply = "Sorry, something went wrong on our side. A staff member will follow up."
                level = "ERROR"
                self.history.append(HumanMessage(text))
            row = self.svc.sm.get(self.appointment_id)
            if root is not None:
                root.update(output={"role": "assistant", "content": reply}, level=level,
                            metadata={"appointment_state": row["state"], "notice_status": row["notice_status"]})
                lf.score_current_trace(name="appointment-state", value=row["state"], data_type="CATEGORICAL")

        log.info("session=%s turn end appt=%s state=%s latency_ms=%d", self.session_id, self.appointment_id,
                 row["state"], (time.perf_counter() - started) * 1000)
        if request_id:
            self._dedupe_put(request_id, reply)
        self._save_message("agent", reply, request_id=request_id, error=self.last_error)
        return reply

    def _save_message(self, role: str, text: str, request_id: str | None = None, error: str | None = None) -> None:
        """Transcript is persisted per message, so any session can be reviewed later from SQLite."""
        row = self.svc.sm.get(self.appointment_id)
        with tx(self.svc.db_path) as c:
            c.execute("INSERT INTO chat_messages(session_id, role, text, appointment_id, state, trace_id, request_id, error, ts)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (self.session_id, role, text, self.appointment_id, row["state"],
                       self.last_trace_id if role == "agent" else None, request_id, error, iso(self.svc.clock.now())))
