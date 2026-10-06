"""Offline UI server test: scripted fake LLM, Langfuse disabled. No Gemini or Langfuse calls."""
import os

os.environ["LANGFUSE_TRACING_ENABLED"] = "false"

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from server.app import create_server

SLOT = "2026-10-07T09:00:00+00:00"   # first dr_rao slot under FakeClock


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c-{name}", "type": "tool_call"}])


class ScriptedLLM:
    """Stands in for Gemini: returns the next scripted message each time the graph asks."""
    def __init__(self, script):
        self.script = list(script)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, config=None):
        return self.script.pop(0)


def test_ui_flow_books_and_checks_pass(tmp_path):
    llm = ScriptedLLM([
        call("verify_patient", full_name="Asha Verma", date_of_birth="1990-04-12"),
        call("set_visit_details", visit_type="checkup", provider_id="dr_rao"),
        call("search_slots"),
        AIMessage(content="I have 9:00, 9:30 and 10:00 with Dr. Rao."),
        call("choose_slot", provider_id="dr_rao", slot_start=SLOT),
        AIMessage(content="Shall I book 9:00 with Dr. Rao?"),
        call("confirm_booking", patient_confirmed=True),
        AIMessage(content="You're booked for 9:00 with Dr. Rao. A confirmation message has been sent."),
    ])
    c = TestClient(create_server(llm=llm, sessions_dir=tmp_path))
    assert "Appointment Scheduler" in c.get("/").text
    assert all(c.get(f"/{f}").status_code == 200 for f in ("app.js", "api.js", "styles.css"))
    assert any(s["id"] == "S1" for s in c.get("/api/scenarios").json())

    sid = c.post("/api/sessions", json={"scenario_id": "S1"}).json()["session_id"]
    for text in ["Hi, I'm Asha Verma, 1990-04-12, checkup with Dr. Rao", "The earliest", "Yes"]:
        r = c.post(f"/api/sessions/{sid}/messages", json={"text": text}).json()
        assert r["error"] is None
    assert r["state"] == "BOOKED"
    st = c.get(f"/api/sessions/{sid}/status").json()
    assert st["appointment"]["notice_status"] == "SENT" and st["langfuse"]["enabled"] is False
    assert any(e["name"] == "state.transition" and e["to_state"] == "BOOKED" for e in st["events"])
    checks = c.post(f"/api/sessions/{sid}/checks", json={}).json()
    assert all(ch["passed"] for group in checks.values() for ch in group), checks


def test_advance_only_for_use_case_sessions(tmp_path):
    c = TestClient(create_server(llm=ScriptedLLM([]), sessions_dir=tmp_path))
    free = c.post("/api/sessions", json={}).json()["session_id"]
    assert c.post(f"/api/sessions/{free}/advance", json={"minutes": 5}).status_code == 400
    s11 = c.post("/api/sessions", json={"scenario_id": "S11"}).json()["session_id"]
    assert c.post(f"/api/sessions/{s11}/advance", json={"minutes": 11}).status_code == 200


def test_history_survives_server_restart(tmp_path):
    llm = ScriptedLLM([call("verify_patient", full_name="Asha Verma", date_of_birth="1990-04-12"),
                       AIMessage(content="Thanks Asha, you're verified.")])
    c = TestClient(create_server(llm=llm, sessions_dir=tmp_path))
    sid = c.post("/api/sessions", json={"scenario_id": "S3"}).json()["session_id"]
    c.post(f"/api/sessions/{sid}/messages", json={"text": "I'm Asha Verma, 1990-04-12"})

    restarted = TestClient(create_server(llm=ScriptedLLM([]), sessions_dir=tmp_path))   # new process, empty memory
    listed = restarted.get("/api/history").json()
    assert [h["session_id"] for h in listed] == [sid] and listed[0]["live"] is False
    detail = restarted.get(f"/api/history/{sid}").json()
    assert [m["role"] for m in detail["transcript"]] == ["user", "agent"]
    assert detail["read_only"] is True and detail["faults"][0]["fault"] == "timeout_after_commit"
    assert any(e["name"] == "tool.call" and e["tool"] == "verify_patient" for e in detail["events"])   # from SQLite
    assert restarted.get("/api/history/..%2Fetc").status_code in (400, 404)
