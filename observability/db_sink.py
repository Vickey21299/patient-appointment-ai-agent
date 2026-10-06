"""EventSink that persists every reliability event to the `events` table of the run's SQLite DB."""
from __future__ import annotations

import json
import logging
from typing import Any

from reliability.clock import Clock, iso
from storage.db import tx

log = logging.getLogger("observability")


class SqliteSink:
    def __init__(self, db_path: str, clock: Clock):
        self.db_path, self.clock = db_path, clock

    def emit(self, name: str, **attrs: Any) -> None:
        try:
            with tx(self.db_path) as c:
                c.execute("INSERT INTO events(ts, name, appointment_id, trace_id, attrs_json) VALUES (?,?,?,?,?)",
                          (iso(self.clock.now()), name, attrs.get("appointment_id"), attrs.get("trace_id"),
                           json.dumps(attrs, default=str)))
        except Exception:  # persistence of telemetry must never break the pipeline
            log.exception("failed to persist event %s", name)
