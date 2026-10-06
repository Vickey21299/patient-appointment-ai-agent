"""Demo data: three patients, two providers, 3 days of 30-minute slots."""
from __future__ import annotations

from datetime import timedelta

from reliability.clock import Clock, iso
from storage.db import tx

PATIENTS = [
    ("p_001", "Asha Verma", "1990-04-12", "+91-90000-00001"),
    ("p_002", "Ravi Kumar", "1985-11-02", "+91-90000-00002"),
    ("p_003", "Meera Iyer", "1958-07-21", "+91-90000-00003"),
]
PROVIDERS = [("dr_rao", "checkup"), ("dr_mehta", "checkup")]


def seed_demo(db_path: str, clock: Clock, days: int = 3) -> None:
    base = clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with tx(db_path) as c:
        c.executemany("INSERT OR IGNORE INTO patients VALUES (?,?,?,?)", PATIENTS)
        for d in range(1, days + 1):
            day = base + timedelta(days=d)
            for hour_block in ((9, 12), (14, 17)):
                t = day.replace(hour=hour_block[0])
                while t.hour < hour_block[1]:
                    for prov, vtype in PROVIDERS:
                        c.execute("INSERT OR IGNORE INTO slots VALUES (?,?,?,?)",
                                  (prov, vtype, iso(t), iso(t + timedelta(minutes=30))))
                    t += timedelta(minutes=30)
