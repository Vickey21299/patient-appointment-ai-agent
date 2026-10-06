"""Print a Langfuse trace as a tree (Langfuse v4 Observations API v2).

  .venv/Scripts/python scripts/inspect_trace.py <trace_id> [--io]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent.config  # noqa: F401  loads .env


def fetch(trace_id: str, hours: int = 24) -> list[dict]:
    now = datetime.now(timezone.utc)
    params = {"traceId": trace_id, "fromStartTime": (now - timedelta(hours=hours)).isoformat(),
              "toStartTime": (now + timedelta(minutes=5)).isoformat(),
              "fields": "core,basic,io,usage,model,metadata,trace_context", "limit": 1000}
    auth = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    r = httpx.get(f"{os.environ['LANGFUSE_BASE_URL']}/api/public/v2/observations", params=params, auth=auth, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace_id")
    ap.add_argument("--io", action="store_true", help="show input/output of each observation")
    a = ap.parse_args()
    obs = sorted(fetch(a.trace_id), key=lambda o: o["startTime"])
    if not obs:
        print("no observations yet (ingestion can take a few seconds)")
        return
    by_id = {o["id"]: o for o in obs}
    root = next((o for o in obs if o.get("isRootObservation")), obs[0])
    print(f"trace={a.trace_id} name={root.get('traceName')} session={root.get('sessionId')} "
          f"user={root.get('userId')} tags={root.get('tags')} env={root.get('environment')} release={root.get('release')}")

    def depth(o):
        d = 0
        while o.get("parentObservationId") in by_id:
            o, d = by_id[o["parentObservationId"]], d + 1
        return d

    for o in obs:
        extra = ""
        if o["type"] == "GENERATION":
            extra = f" model={o.get('model')} usage={o.get('usageDetails')}"
        if o.get("level") not in (None, "DEFAULT"):
            extra += f" level={o['level']} msg={o.get('statusMessage')}"
        print("  " * depth(o) + f"- [{o['type']}] {o.get('name')}{extra}")
        if a.io:
            for k in ("input", "output"):
                if o.get(k) is not None:
                    print("  " * depth(o) + f"    {k}: {json.dumps(o[k], ensure_ascii=False)[:300]}")


if __name__ == "__main__":
    main()
