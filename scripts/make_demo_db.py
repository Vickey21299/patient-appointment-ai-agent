"""Builds demo.db by running a few scenarios through the real service, so the tables have
realistic content to inspect. Usage: .venv/Scripts/python scripts/make_demo_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import create_app
from backend.injector import Fault, FaultInjectingTransport, inprocess_transport
from backend.seed import seed_demo
from gateway.tools import ToolGateway
from reliability.clock import FakeClock
from reliability.events import ListSink
from reliability.policy import load_policy
from reliability.service import SchedulingService
from storage.db import init_db

DB = Path(__file__).resolve().parents[1] / "demo.db"


def main() -> None:
    DB.unlink(missing_ok=True)
    clock = FakeClock()
    init_db(DB)
    seed_demo(str(DB), clock)
    inj = FaultInjectingTransport(inprocess_transport(create_app(str(DB), clock)), db_path=str(DB), clock=clock)
    svc = SchedulingService(str(DB), ToolGateway(httpx.Client(base_url="http://backend", transport=inj)),
                            load_policy(), clock, ListSink())

    def offered(name, dob, provider="dr_rao", trace="t"):
        svc.trace_id = trace
        a = svc.start()
        svc.verify_patient(a, name, dob)
        svc.set_details(a, visit_type="checkup", provider_id=provider)
        return a, svc.search(a).data["slots"]

    # S1 happy path: Asha books slot 0
    a, s = offered("Asha Verma", "1990-04-12", trace="trace-S1")
    svc.choose_slot(a, s[0]["provider_id"], s[0]["slot_start"]); svc.confirm(a, True)

    # S3 response lost after commit: Ravi books slot 1, recovered by read-back
    a, s = offered("Ravi Kumar", "1985-11-02", trace="trace-S3")
    svc.choose_slot(a, s[0]["provider_id"], s[0]["slot_start"])
    inj.faults.append(Fault("book_appointment", "timeout_after_commit", inj.counts.get("book_appointment", 0) + 1)); svc.confirm(a, True)

    # S2 slot stolen: Meera tries a slot someone else grabs, then takes an alternative
    a, s = offered("Meera Iyer", "1958-07-21", provider="dr_mehta", trace="trace-S2")
    inj.faults.append(Fault("hold_slot", "steal_slot", inj.counts.get("hold_slot", 0) + 1))
    alt = svc.choose_slot(a, s[0]["provider_id"], s[0]["slot_start"]).data["alternatives"][0]
    svc.choose_slot(a, alt["provider_id"], alt["slot_start"]); svc.confirm(a, True)

    # S9 reschedule where cancelling the old booking fails -> ESCALATED (DUPLICATE_LIVE)
    a, s = offered("Asha Verma", "1990-04-12", provider="dr_mehta", trace="trace-S9")
    svc.choose_slot(a, s[2]["provider_id"], s[2]["slot_start"]); svc.confirm(a, True)
    svc.request_reschedule(a)
    new = svc.reschedule_search(a).data["slots"][3]
    svc.reschedule_choose(a, new["provider_id"], new["slot_start"])
    inj.faults.append(Fault("cancel_appointment", "error_503"))
    svc.reschedule_confirm(a, True)
    inj.faults.clear()

    # Abandoned draft that times out (T25)
    a, s = offered("Ravi Kumar", "1985-11-02", trace="trace-T25")
    clock.advance(minutes=31); svc.tick()

    print(f"wrote {DB}")


if __name__ == "__main__":
    main()
