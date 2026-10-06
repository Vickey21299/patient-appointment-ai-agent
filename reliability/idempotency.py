"""Write-ahead intent log and deterministic idempotency keys (I2, I4)."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from reliability.clock import Clock, iso
from storage.db import tx


def make_key(appointment_id: str, op: str, args: dict[str, Any]) -> str:
    """Same logical operation gives the same key. The key never includes an attempt counter."""
    canon = json.dumps({"a": appointment_id, "op": op, "args": args}, sort_keys=True, separators=(",", ":"))
    return f"{op}-{hashlib.sha256(canon.encode()).hexdigest()[:24]}"


class IntentLog:
    def __init__(self, db_path: str, clock: Clock):
        self.db_path, self.clock = db_path, clock

    def record(self, appointment_id: str, op: str, key: str, args: dict[str, Any]) -> dict[str, Any]:
        """Write the intent BEFORE the call (I4). Re-recording the same key returns the existing row."""
        with tx(self.db_path) as c:
            c.execute(
                "INSERT OR IGNORE INTO intents(id, appointment_id, op, idempotency_key, args_json, status, created_at)"
                " VALUES (?,?,?,?,?, 'PENDING', ?)",
                (f"in_{uuid.uuid4().hex[:10]}", appointment_id, op, key, json.dumps(args), iso(self.clock.now())),
            )
            c.execute("UPDATE intents SET attempts = attempts + 1 WHERE idempotency_key=?", (key,))
            return dict(c.execute("SELECT * FROM intents WHERE idempotency_key=?", (key,)).fetchone())

    def mark(self, key: str, status: str, result: dict[str, Any] | None = None) -> None:
        resolved = None if status in ("PENDING", "UNKNOWN") else iso(self.clock.now())
        with tx(self.db_path) as c:
            c.execute("UPDATE intents SET status=?, result_json=COALESCE(?, result_json), resolved_at=? "
                      "WHERE idempotency_key=?", (status, json.dumps(result) if result else None, resolved, key))

    def get(self, key: str) -> dict[str, Any] | None:
        with tx(self.db_path) as c:
            r = c.execute("SELECT * FROM intents WHERE idempotency_key=?", (key,)).fetchone()
        return dict(r) if r else None
