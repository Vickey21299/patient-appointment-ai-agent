"""Chat with the agent in the terminal.

  .venv/Scripts/python -m agent.cli                 interactive
  .venv/Scripts/python -m agent.cli --demo          scripted happy-path conversation
  .venv/Scripts/python -m agent.cli --demo --fault book_appointment:timeout_after_commit   (S3 live)
"""
from __future__ import annotations

import argparse
import sys

from agent.runtime import build_runtime

DEMO_TURNS = [
    "Hi, I'd like to book a checkup with Dr. Rao please.",
    "My name is Asha Verma, date of birth 1990-04-12.",
    "Any morning slot is fine, the earliest one works.",
    "Yes, please book it.",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--fault", action="append", default=[], help="tool:fault[:call], e.g. book_appointment:timeout_after_commit:1")
    args = ap.parse_args()

    rt = build_runtime()
    for spec in args.fault:
        tool, fault, *call = spec.split(":")
        rt.inject(tool, fault, int(call[0]) if call else "all")
    session = rt.new_session(tags=["cli", "demo" if args.demo else "interactive"])
    print(f"session {session.session_id}  (logs/ has the pipeline log; Ctrl+C to quit)\n")
    try:
        turns = DEMO_TURNS if args.demo else iter(lambda: input("you> "), None)
        for text in turns:
            if args.demo:
                print(f"you> {text}")
            if not text.strip():
                continue
            print(f"agent> {session.handle(text)}\n")
            if rt.langfuse and session.last_trace_id:
                print(f"       trace: {rt.langfuse.get_trace_url(trace_id=session.last_trace_id)}\n")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        rt.flush()
        state = rt.svc.sm.get(session.appointment_id)["state"]
        print(f"\nfinal appointment state: {state}")


if __name__ == "__main__":
    sys.exit(main())
