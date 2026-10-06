"""Pipeline status at a glance.  .venv/Scripts/python -m observability.status [--db app.db] [--lines 20]"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from storage.db import query

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "app.db"))
    ap.add_argument("--log-dir", default=str(ROOT / "logs"))
    ap.add_argument("--lines", type=int, default=20)
    a = ap.parse_args()

    if not Path(a.db).exists():
        print(f"no database at {a.db}")
        return
    print(f"== Appointments by state ({a.db})")
    for r in query(a.db, "SELECT state, COUNT(*) n FROM appointments GROUP BY state ORDER BY n DESC"):
        print(f"   {r['state']:<12} {r['n']}")

    print("\n== In flight / needs attention")
    rows = query(a.db, "SELECT id, state, updated_at FROM appointments "
                       "WHERE state IN ('COMMITTING','RECONCILING','CANCELLING','MODIFYING') ORDER BY updated_at")
    print("\n".join(f"   {r['id']} {r['state']} since {r['updated_at'][:19]}" for r in rows) or "   none")

    print("\n== Escalations (latest 5)")
    for r in query(a.db, "SELECT appointment_id, reason, created_at, packet_json FROM escalations ORDER BY created_at DESC LIMIT 5"):
        nxt = json.loads(r["packet_json"]).get("suggested_next_step", "")
        print(f"   {r['created_at'][:19]} {r['appointment_id']} {r['reason']} -> {nxt}")

    print("\n== Notifications not sent")
    rows = query(a.db, "SELECT appointment_id, kind, status, attempts FROM outbox WHERE status <> 'SENT'")
    print("\n".join(f"   {r['appointment_id']} {r['kind']} {r['status']} attempts={r['attempts']}" for r in rows) or "   none")

    print("\n== Unresolved write intents")
    rows = query(a.db, "SELECT appointment_id, op, status, attempts FROM intents WHERE status IN ('PENDING','UNKNOWN')")
    print("\n".join(f"   {r['appointment_id']} {r['op']} {r['status']} attempts={r['attempts']}" for r in rows) or "   none")

    log = Path(a.log_dir) / f"pipeline_{date.today().isoformat()}.log"
    print(f"\n== Last {a.lines} log lines ({log.name})")
    if log.exists():
        lines = log.read_text(encoding="utf-8").splitlines()[-a.lines:]
        print("\n".join(f"   {line}" for line in lines))
    else:
        print("   no log for today yet")


if __name__ == "__main__":
    main()
