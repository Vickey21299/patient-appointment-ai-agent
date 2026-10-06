"""Runs one scenario once against the real LangGraph + Gemini agent.

Each run gets a fresh SQLite DB, a FakeClock (deterministic dates, instant backoff, time-warp),
and its own Langfuse session, so all of its turn-traces group together.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent.runtime import build_runtime
from eval.checks import TurnRecord, active_bookings, run_checks
from eval.scenario import Scenario
from reliability.clock import FakeClock

log = logging.getLogger("eval.harness")
RUNS_DIR = Path(__file__).with_name(".runs")


@dataclass
class RunResult:
    scenario_id: str
    run_index: int
    session_id: str
    status: str                       # pass | fail | error (infra: API errors, crashes)
    checks: dict[str, list[dict[str, Any]]]
    turns: list[dict[str, Any]]
    final_state: str
    duration_s: float
    error: str | None = None
    trace_ids: list[str] = field(default_factory=list)

    @property
    def failed_checks(self) -> list[str]:
        return [f"{g}/{c['name']}" for g, cs in self.checks.items() for c in cs if not c["passed"]]


def arm_faults(rt, faults: list[dict[str, Any]]) -> None:
    """Call numbers in YAML are relative to the moment the fault is armed."""
    for f in faults:
        calls = f.get("calls", "all")
        if calls != "all":
            base = rt.injector.counts.get(f["tool"], 0)
            calls = [base + int(n) for n in (calls if isinstance(calls, list) else [calls])]
        rt.inject(f["tool"], f["fault"], calls)


def run_scenario(sc: Scenario, run_index: int, llm, eval_run_id: str, score: bool = True) -> RunResult:
    started = time.perf_counter()
    db = RUNS_DIR / eval_run_id / f"{sc.id}_r{run_index}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)
    clock = FakeClock()
    rt = build_runtime(str(db), clock=clock, llm=llm)

    appointment_id = patient_id = None
    if pb := sc.setup.get("prebook"):
        appointment_id, patient_id = rt.prebook(tuple(pb["patient"]), pb.get("provider_id", "dr_rao"),
                                                pb.get("slot_index", 0))
    arm_faults(rt, sc.faults)

    session_id = f"eval-{eval_run_id}-{sc.id}-r{run_index}"
    session = rt.new_session(tags=["eval", f"scenario:{sc.id}", f"failure-class:{sc.primary_failure_class}",
                                   f"eval-run:{eval_run_id}"],
                             session_id=session_id, appointment_id=appointment_id, patient_id=patient_id)
    turns: list[TurnRecord] = []
    error = None
    for t in sc.turns:
        if t.advance_minutes:
            clock.advance(minutes=t.advance_minutes)
            rt.svc.tick()
        n_before = len(rt.events.events)
        reply = session.handle(t.user, request_id=t.request_id)
        row = rt.svc.sm.get(session.appointment_id)
        turns.append(TurnRecord(t.user, reply, row["state"], row["notice_status"], active_bookings(str(db)),
                                session.last_trace_id, session.last_error, rt.events.events[n_before:]))
        if session.last_error:
            error = session.last_error
            break

    row = rt.svc.sm.get(session.appointment_id)
    checks = run_checks(sc.expect, db=str(db), injector=rt.injector, events=rt.events.events, turns=turns,
                        final_state=row["state"], notice_status=row["notice_status"])
    passed = all(c.passed for cs in checks.values() for c in cs)
    status = "error" if error else ("pass" if passed else "fail")

    if score and rt.langfuse is not None:
        _score_session(rt.langfuse, session_id, checks, status, sc)
        rt.flush()

    result = RunResult(
        scenario_id=sc.id, run_index=run_index, session_id=session_id, status=status,
        checks={g: [asdict(c) for c in cs] for g, cs in checks.items()},
        turns=[{k: v for k, v in asdict(t).items() if k != "events"} for t in turns],
        final_state=row["state"], duration_s=round(time.perf_counter() - started, 1), error=error,
        trace_ids=[t.trace_id for t in turns if t.trace_id],
    )
    log.info("scenario=%s run=%d status=%s failed=%s", sc.id, run_index, status, result.failed_checks)
    return result


def _score_session(lf, session_id: str, checks, status: str, sc: Scenario) -> None:
    """Session-level scores: one per check group plus the overall result."""
    meta = {"scenario_id": sc.id, "failure_class": sc.primary_failure_class}
    for group, cs in checks.items():
        failed = [f"{c.name}: {c.detail}" for c in cs if not c.passed]
        lf.create_score(session_id=session_id, name=group, value=0 if failed else 1, data_type="BOOLEAN",
                        comment="; ".join(failed)[:1000] or None, metadata=meta)
    lf.create_score(session_id=session_id, name="scenario-result", value=status, data_type="CATEGORICAL", metadata=meta)
