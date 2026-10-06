import sqlite3

import pytest

from reliability.clock import FakeClock
from reliability.events import ListSink
from reliability.state_machine import TERMINAL, TRANSITIONS, IllegalTransition, State, StateMachine
from storage.db import connect, init_db


@pytest.fixture
def sm(tmp_path):
    db = str(tmp_path / "sm.db")
    init_db(db)
    return StateMachine(db, FakeClock(), ListSink())


def test_every_transition_targets_a_known_state_and_none_leave_terminal():
    for (src, _event), (tid, target) in TRANSITIONS.items():
        assert src not in TERMINAL, f"{tid} leaves a terminal state"
        targets = target.values() if isinstance(target, dict) else [target]
        assert all(isinstance(t, State) for t in targets)


def test_illegal_transition_rejected_and_logged_F9(sm):
    a = sm.create()
    with pytest.raises(IllegalTransition):
        sm.apply(a, "book_ok")
    assert sm.sink.of("transition.rejected")[0]["failure_class"] == "F9"
    assert sm.get(a)["state"] == "COLLECTING"


def test_fields_complete_requires_verified_patient_I8(sm):
    a = sm.create()
    sm.update(a, visit_type="checkup")
    with pytest.raises(IllegalTransition, match="I8"):
        sm.apply(a, "fields_complete")
    sm.update(a, patient_verified=1)
    assert sm.apply(a, "fields_complete")["state"] == "SEARCHING"


def test_transition_is_recorded_with_trace_id_I10(sm):
    a = sm.create()
    sm.update(a, visit_type="checkup", patient_verified=1)
    sm.apply(a, "fields_complete", trace_id="t-1")
    ev = sm.sink.of("state.transition")[0]
    assert (ev["from_state"], ev["to_state"], ev["trace_id"], ev["transition"]) == ("COLLECTING", "SEARCHING", "t-1", "T01")
    conn = connect(sm.db_path)
    row = conn.execute("SELECT * FROM transitions WHERE appointment_id=?", (a,)).fetchone()
    assert row["trace_id"] == "t-1" and row["cause"].startswith("T01")


def test_version_conflict_rejected(sm):
    a = sm.create()
    sm.update(a, visit_type="checkup", patient_verified=1)
    with pytest.raises(IllegalTransition, match="version"):
        sm.apply(a, "fields_complete", expected_version=99)


def test_terminal_state_is_immutable_even_with_raw_sql_I9(sm):
    a = sm.create()
    sm.apply(a, "patient_abandon")
    with pytest.raises(IllegalTransition):
        sm.apply(a, "escalate")
    conn = connect(sm.db_path)
    with pytest.raises(sqlite3.IntegrityError, match="I9"):
        conn.execute("UPDATE appointments SET state='BOOKED' WHERE id=?", (a,))


def test_op_dependent_target_requires_op(sm):
    a = sm.create()
    with sqlite3.connect(sm.db_path) as c:
        c.execute("UPDATE appointments SET state='RECONCILING' WHERE id=?", (a,))
    with pytest.raises(IllegalTransition, match="op"):
        sm.apply(a, "readback_applied")
    assert sm.apply(a, "readback_applied", op="cancel")["state"] == "CANCELLED"
