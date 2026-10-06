"""Scripted failure injection (RELIABILITY_SPEC §8). Test mode only.

It sits on the HTTP transport between gateway and backend, so a fault looks to the agent
exactly like a real network or server failure. `timeout_after_commit` really commits on
the backend and then drops the response. That is the F2 case.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

import httpx

from reliability.clock import Clock, iso
from storage.db import tx

ROUTES = [
    ("GET", r"^/slots$", "search_slots"),
    ("POST", r"^/holds$", "hold_slot"),
    ("DELETE", r"^/holds/[^/]+$", "release_hold"),
    ("POST", r"^/appointments$", "book_appointment"),
    ("POST", r"^/appointments/[^/]+/cancel$", "cancel_appointment"),
    ("GET", r"^/appointments/[^/]+$", "get_appointment"),
    ("GET", r"^/bookings/by-key/[^/]+$", "get_by_key"),
    ("POST", r"^/patients/verify$", "verify_patient"),
    ("POST", r"^/notifications$", "send_notification"),
]

FAULTS = {"error_503", "conflict_409", "timeout_before_commit", "timeout_after_commit",
          "connect_error", "steal_slot"}


def tool_for(method: str, path: str) -> str:
    for m, pat, name in ROUTES:
        if m == method and re.match(pat, path):
            return name
    return "unknown"


@dataclass
class Fault:
    tool: str
    fault: str
    calls: list[int] | str = "all"     # 1-based call numbers for that tool, or "all"

    def __post_init__(self):
        if self.fault not in FAULTS:
            raise ValueError(f"unknown fault {self.fault!r}")
        if isinstance(self.calls, int):
            self.calls = [self.calls]

    def applies(self, n: int) -> bool:
        return self.calls == "all" or n in self.calls


@dataclass
class CallRecord:
    tool: str
    n: int
    fault: str | None
    method: str
    path: str


class FaultInjectingTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport, faults: list[Fault] | None = None,
                 db_path: str | None = None, clock: Clock | None = None):
        self.inner, self.faults = inner, list(faults or [])
        self.db_path, self.clock = db_path, clock
        self.counts: dict[str, int] = {}
        self.calls: list[CallRecord] = []

    def calls_to(self, tool: str) -> list[CallRecord]:
        return [c for c in self.calls if c.tool == tool]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        tool = tool_for(request.method, request.url.path)
        n = self.counts[tool] = self.counts.get(tool, 0) + 1
        fault = next((f.fault for f in self.faults if f.tool == tool and f.applies(n)), None)
        self.calls.append(CallRecord(tool, n, fault, request.method, request.url.path))

        if fault == "error_503":
            return httpx.Response(503, json={"error": "injected_503"}, request=request)
        if fault == "conflict_409":
            return httpx.Response(409, json={"detail": {"error": "slot_taken"}}, request=request)
        if fault == "connect_error":
            raise httpx.ConnectError("injected connect error", request=request)
        if fault == "timeout_before_commit":
            raise httpx.ReadTimeout("injected timeout (not committed)", request=request)
        if fault == "steal_slot":
            self._steal(json.loads(request.content))
        response = self.inner.handle_request(request)
        if fault == "timeout_after_commit":
            response.read()
            raise httpx.ReadTimeout("injected timeout (committed, response lost)", request=request)
        return response

    def _steal(self, body: dict) -> None:
        """Another patient grabs the slot just before our hold arrives (S2)."""
        assert self.db_path and self.clock, "steal_slot needs db_path and clock"
        t = iso(self.clock.now())
        with tx(self.db_path) as c:
            c.execute(
                "INSERT OR IGNORE INTO bookings(id, kind, status, patient_id, provider_id, slot_start,"
                " idempotency_key, created_at, updated_at) VALUES (?, 'BOOKING', 'ACTIVE', 'p_other', ?, ?, ?, ?, ?)",
                (f"bk_{uuid.uuid4().hex[:10]}", body["provider_id"], body["slot_start"],
                 f"steal-{uuid.uuid4().hex}", t, t),
            )


def inprocess_transport(app) -> httpx.BaseTransport:
    """Sync transport that serves requests from the FastAPI app in-process (no network)."""
    from fastapi.testclient import TestClient

    tc = TestClient(app, raise_server_exceptions=True)

    def handler(request: httpx.Request) -> httpx.Response:
        r = tc.request(request.method, str(request.url.path), params=request.url.params,
                       content=request.content, headers={"content-type": "application/json"})
        return httpx.Response(r.status_code, content=r.content, headers=r.headers, request=request)

    return httpx.MockTransport(handler)
