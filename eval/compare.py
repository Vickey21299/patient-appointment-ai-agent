"""Regression proof: compare two eval reports.

  .venv/Scripts/python -m eval.compare eval/reports/baseline.json eval/reports/<new>.json
Exit code 1 if any scenario regressed (usable as a CI gate).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def compare(before: Path, after: Path, tolerance: float = 0.0) -> list[str]:
    """Print the before/after table and return the scenario ids that regressed."""
    b, n = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (before, after))
    bs = {r["scenario_id"]: r for r in b["summary"]["scenarios"]}
    ns = {r["scenario_id"]: r for r in n["summary"]["scenarios"]}

    print(f"before: {b['meta']['eval_run_id']} (release {b['meta']['release']})   "
          f"after: {n['meta']['eval_run_id']} (release {n['meta']['release']})\n")
    print(f"{'scenario':<9}{'before':>8}{'after':>8}{'delta':>8}  verdict")
    regressed = []
    for sid in sorted(set(bs) | set(ns), key=lambda s: int(s[1:]) if s[1:].isdigit() else 0):
        pb = bs.get(sid, {}).get("pass_rate")
        pn = ns.get(sid, {}).get("pass_rate")
        if pb is None or pn is None:
            print(f"{sid:<9}{'-' if pb is None else f'{pb:.0%}':>8}{'-' if pn is None else f'{pn:.0%}':>8}{'':>8}  not comparable")
            continue
        d = pn - pb
        verdict = ("FIXED" if pb < 1 and pn == 1 else "REGRESSED" if d < -tolerance
                   else "improved" if d > 0 else "same")
        if verdict == "REGRESSED":
            regressed.append(sid)
        print(f"{sid:<9}{pb:>8.0%}{pn:>8.0%}{d:>+8.0%}  {verdict}")
    ob, on = b["summary"]["overall_pass_rate"], n["summary"]["overall_pass_rate"]
    print(f"\noverall: {ob:.1%} -> {on:.1%}")
    if regressed:
        print(f"REGRESSIONS: {regressed}")
    return regressed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--tolerance", type=float, default=0.0, help="allowed pass-rate drop before flagging")
    a = ap.parse_args()
    if compare(a.before, a.after, a.tolerance):
        sys.exit(1)


if __name__ == "__main__":
    main()
