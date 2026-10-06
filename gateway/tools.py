"""Tool Gateway: typed, deadline-bounded calls to the backend.

It is deliberately dumb. It never retries and never classifies; it reports what happened as a
ToolResult. Classification and retry belong to reliability/policy.py and reliability/executor.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx

WRITE_TOOLS = {"hold_slot", "book_appointment", "cancel_appointment", "send_notification", "release_hold"}


@dataclass
class ToolResult:
    tool: str
    status: int | None = None           # HTTP status, None if no response
    body: dict[str, Any] = field(default_factory=dict)
    error: str | None = None            # None | "timeout" | "connect"

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    @property
    def is_write(self) -> bool:
        return self.tool in WRITE_TOOLS

    @property
    def error_code(self) -> str | None:
        d = self.body.get("detail") if isinstance(self.body, dict) else None
        return d.get("error") if isinstance(d, dict) else self.body.get("error")


class ToolGateway:
    def __init__(self, client: httpx.Client, deadline_s: float = 5.0):
        self.client, self.deadline_s = client, deadline_s

    def _call(self, tool: str, method: str, path: str, **kw) -> ToolResult:
        try:
            r = self.client.request(method, path, timeout=self.deadline_s, **kw)
        except httpx.TimeoutException:
            return ToolResult(tool, error="timeout")
        except httpx.TransportError:
            return ToolResult(tool, error="connect")
        try:
            body = r.json()
        except ValueError:
            body = {}
        return ToolResult(tool, status=r.status_code, body=body if isinstance(body, dict) else {"data": body})

    # --- reads ---
    def verify_patient(self, name: str, dob: str) -> ToolResult:
        return self._call("verify_patient", "POST", "/patients/verify", json={"name": name, "dob": dob})

    def search_slots(self, **filters: str | None) -> ToolResult:
        params = {k: v for k, v in filters.items() if v}
        if (d := params.get("date_to")) and len(d) == 10:
            # IMP-001: the tool contract's date_to is an inclusive calendar date; the backend's is an
            # exclusive timestamp bound. Without this, date_from == date_to (one day) matched nothing.
            params["date_to"] = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
        return self._call("search_slots", "GET", "/slots", params=params)

    def get_by_key(self, key: str) -> ToolResult:
        return self._call("get_by_key", "GET", f"/bookings/by-key/{key}")

    def get_appointment(self, booking_id: str) -> ToolResult:
        return self._call("get_appointment", "GET", f"/appointments/{booking_id}")

    # --- writes (always carry an idempotency key) ---
    def hold_slot(self, *, patient_id: str, provider_id: str, slot_start: str, ttl_seconds: int, key: str) -> ToolResult:
        return self._call("hold_slot", "POST", "/holds", json={
            "patient_id": patient_id, "provider_id": provider_id, "slot_start": slot_start,
            "ttl_seconds": ttl_seconds, "idempotency_key": key})

    def release_hold(self, hold_id: str) -> ToolResult:
        return self._call("release_hold", "DELETE", f"/holds/{hold_id}")

    def book_appointment(self, *, hold_id: str, key: str) -> ToolResult:
        return self._call("book_appointment", "POST", "/appointments", json={"hold_id": hold_id, "idempotency_key": key})

    def cancel_appointment(self, *, booking_id: str, key: str) -> ToolResult:
        return self._call("cancel_appointment", "POST", f"/appointments/{booking_id}/cancel", json={"idempotency_key": key})

    def send_notification(self, *, booking_id: str, kind: str, key: str) -> ToolResult:
        return self._call("send_notification", "POST", "/notifications",
                          json={"booking_id": booking_id, "kind": kind, "idempotency_key": key})
