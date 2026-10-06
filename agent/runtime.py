"""Wires the whole stack: logging -> Langfuse -> backend -> reliability service -> Gemini agent."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from agent.config import settings  # loads .env first
from backend.app import create_app
from backend.injector import Fault, FaultInjectingTransport, inprocess_transport
from backend.seed import seed_demo
from gateway.tools import ToolGateway
from observability.langfuse_tracing import LangfuseSink, LangfuseTracer, init_langfuse
from observability.db_sink import SqliteSink
from observability.log_sink import LogSink
from observability.logging_setup import setup_logging
from reliability.clock import Clock
from reliability.events import FanoutSink, ListSink
from reliability.policy import load_policy
from reliability.service import SchedulingService
from storage.db import init_db

log = logging.getLogger("agent.runtime")


@dataclass
class Runtime:
    svc: SchedulingService
    injector: FaultInjectingTransport
    langfuse: object | None
    events: ListSink
    llm: object = field(repr=False, default=None)

    def new_session(self, **kw):
        from agent.session import ChatSession
        return ChatSession(self.svc, self.llm, self.langfuse, settings.recursion_limit, **kw)

    def prebook(self, patient: tuple[str, str], provider_id: str = "dr_rao", slot_index: int = 0) -> tuple[str, str]:
        """Create a BOOKED appointment directly through the service (scenario setup). Returns (appointment_id, patient_id)."""
        svc = self.svc
        a = svc.start()
        pid = svc.verify_patient(a, *patient).data["patient_id"]
        svc.set_details(a, visit_type="checkup", provider_id=provider_id)
        s = svc.search(a).data["slots"][slot_index]
        svc.choose_slot(a, s["provider_id"], s["slot_start"])
        assert svc.confirm(a, True).kind == "BOOKED"
        return a, pid

    def inject(self, tool: str, fault: str, calls="all") -> None:
        self.injector.faults.append(Fault(tool, fault, calls))

    def flush(self) -> None:
        if self.langfuse is not None:
            self.langfuse.flush()


def build_llm():
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(
        model=settings.model, temperature=settings.temperature,
        thinking_budget=settings.thinking_budget, include_thoughts=True,   # thinking captured in traces
        max_retries=2, timeout=60,
    )


def build_runtime(db_path: str | None = None, clock: Clock | None = None, llm=None) -> Runtime:
    setup_logging(settings.log_dir)
    clock = clock or Clock()
    db_path = db_path or settings.db_path
    fresh = not Path(db_path).exists()
    init_db(db_path)
    seed_demo(db_path, clock)            # idempotent: INSERT OR IGNORE
    lf = init_langfuse()
    events = ListSink()
    sinks = [events, SqliteSink(db_path, clock), LogSink()] + ([LangfuseSink(lf)] if lf else [])
    tracer = LangfuseTracer(lf) if lf else None
    # In-process mock backend behind the fault-injecting transport (no faults unless inject() is called).
    injector = FaultInjectingTransport(inprocess_transport(create_app(db_path, clock)), db_path=db_path, clock=clock)
    gateway = ToolGateway(httpx.Client(base_url="http://backend", transport=injector))
    svc = SchedulingService(db_path, gateway, load_policy(), clock, FanoutSink(*sinks), tracer=tracer)
    log.info("runtime ready db=%s fresh=%s model=%s tracing=%s", db_path, fresh, settings.model, bool(lf))
    return Runtime(svc, injector, lf, events, llm or build_llm())
