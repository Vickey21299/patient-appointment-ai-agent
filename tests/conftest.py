from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest

from backend.app import create_app
from eval.invariants import check_invariants
from backend.injector import Fault, FaultInjectingTransport, inprocess_transport
from backend.seed import seed_demo
from gateway.tools import ToolGateway
from reliability.clock import FakeClock
from reliability.events import ListSink
from reliability.policy import load_policy
from reliability.service import SchedulingService
from storage.db import init_db, query

ASHA = ("Asha Verma", "1990-04-12")


@dataclass
class Env:
    db: str
    clock: FakeClock
    sink: ListSink
    injector: FaultInjectingTransport
    svc: SchedulingService

    def fault(self, tool: str, fault: str, calls="all") -> None:
        self.injector.faults.append(Fault(tool, fault, calls))

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in query(self.db, sql, params)]

    def active_bookings(self) -> list[dict]:
        return self.rows("SELECT * FROM bookings WHERE status='ACTIVE' AND kind='BOOKING' AND patient_id<>'p_other'")

    def to_offered(self, patient=ASHA, provider_id="dr_rao"):
        a = self.svc.start()
        assert self.svc.verify_patient(a, *patient).kind == "VERIFIED"
        self.svc.set_details(a, visit_type="checkup", provider_id=provider_id)
        out = self.svc.search(a)
        assert out.kind == "SLOTS", out
        return a, out.data["slots"]

    def to_confirming(self, patient=ASHA, slot_index=0):
        a, slots = self.to_offered(patient)
        s = slots[slot_index]
        out = self.svc.choose_slot(a, s["provider_id"], s["slot_start"])
        assert out.kind == "HELD", out
        return a, s

    def assert_invariants(self) -> None:
        """Checks I1, I2, I4, I6, I12 against the DB. Every scenario test calls it."""
        failed = [c for c in check_invariants(self.db, self.injector) if not c.passed]
        assert not failed, "; ".join(f"{c.name}: {c.detail}" for c in failed)


@pytest.fixture
def env(tmp_path) -> Env:
    db = str(tmp_path / "test.db")
    clock = FakeClock()
    init_db(db)
    seed_demo(db, clock)
    injector = FaultInjectingTransport(inprocess_transport(create_app(db, clock)), db_path=db, clock=clock)
    client = httpx.Client(base_url="http://backend", transport=injector)
    sink = ListSink()
    svc = SchedulingService(db, ToolGateway(client), load_policy(), clock, sink)
    svc.trace_id = "trace-test"
    return Env(db, clock, sink, injector, svc)
