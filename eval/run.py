"""Run the scenario matrix.

  .venv/Scripts/python -m eval.run                        all scenarios x 3 runs
  .venv/Scripts/python -m eval.run -s S1 S3 -n 1          subset
  .venv/Scripts/python -m eval.run --label baseline       also saves eval/reports/baseline.json
  .venv/Scripts/python -m eval.run --no-scores            don't write scores to Langfuse
"""
from __future__ import annotations

import os

os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", "evaluation")   # keep eval traces out of dev/prod views
os.environ.setdefault("LOG_CONSOLE_LEVEL", "ERROR")                 # injected faults are expected; see logs/

import argparse
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from agent.config import settings
from agent.runtime import build_llm
from eval.compare import compare
from eval.harness import RunResult, run_scenario
from eval.scenario import load_all
from observability.langfuse_tracing import AGENT_RELEASE

REPORTS = Path(__file__).with_name("reports")


def summarize(results: list[RunResult], scenarios) -> dict:
    by_sc: dict[str, list[RunResult]] = {}
    for r in results:
        by_sc.setdefault(r.scenario_id, []).append(r)
    rows = []
    for sc in scenarios:
        rs = sorted(by_sc.get(sc.id, []), key=lambda r: r.run_index)
        scored = [r for r in rs if r.status != "error"]
        failed_checks: dict[str, int] = {}
        for r in rs:
            for c in r.failed_checks:
                failed_checks[c] = failed_checks.get(c, 0) + 1
        rows.append({
            "scenario_id": sc.id, "title": sc.title, "failure_class": sc.primary_failure_class,
            "runs": [r.status for r in rs],
            "pass_rate": round(sum(r.status == "pass" for r in scored) / len(scored), 3) if scored else None,
            "errors": sum(r.status == "error" for r in rs),
            "failed_checks": failed_checks,
        })
    scored_all = [r for r in results if r.status != "error"]
    return {
        "overall_pass_rate": round(sum(r.status == "pass" for r in scored_all) / len(scored_all), 3) if scored_all else None,
        "runs_total": len(results), "runs_errored": sum(r.status == "error" for r in results),
        "scenarios": rows,
    }


def print_matrix(summary: dict) -> None:
    print(f"\n{'scenario':<9}{'class':<7}{'runs':<22}{'pass':>6}  failing checks")
    print("-" * 100)
    for row in summary["scenarios"]:
        runs = " ".join({"pass": "PASS", "fail": "FAIL", "error": "ERR "}[s] for s in row["runs"])
        rate = "-" if row["pass_rate"] is None else f"{row['pass_rate']:.0%}"
        fc = ", ".join(f"{k} x{v}" for k, v in row["failed_checks"].items())
        print(f"{row['scenario_id']:<9}{row['failure_class']:<7}{runs:<22}{rate:>6}  {fc}")
    print("-" * 100)
    o = summary["overall_pass_rate"]
    print(f"overall pass rate: {'-' if o is None else f'{o:.1%}'}  "
          f"(runs={summary['runs_total']}, infra errors={summary['runs_errored']})\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--scenarios", nargs="*")
    ap.add_argument("-n", "--runs", type=int, default=3)
    ap.add_argument("-w", "--workers", type=int, default=2)
    ap.add_argument("--label", help="also save the report as eval/reports/<label>.json (e.g. baseline)")
    ap.add_argument("--no-scores", action="store_true")
    ap.add_argument("--compare", metavar="REPORT",
                    help="after the run, compare against this report (e.g. eval/reports/baseline.json); exit 1 on regression")
    a = ap.parse_args()

    scenarios = load_all(a.scenarios)
    if not scenarios:
        sys.exit("no scenarios matched")
    eval_run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    llm = build_llm()
    jobs = [(sc, i) for sc in scenarios for i in range(1, a.runs + 1)]
    print(f"eval run {eval_run_id}: {len(scenarios)} scenarios x {a.runs} runs = {len(jobs)} runs, "
          f"model={settings.model}, workers={a.workers}")

    started = time.perf_counter()
    results: list[RunResult] = []
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(run_scenario, sc, i, llm, eval_run_id, not a.no_scores): (sc, i) for sc, i in jobs}
        for f in as_completed(futs):
            sc, i = futs[f]
            try:
                r = f.result()
            except Exception as e:   # harness bug, not agent behaviour
                r = RunResult(sc.id, i, "-", "error", {}, [], "-", 0, error=f"{type(e).__name__}: {e}")
            results.append(r)
            print(f"  {r.scenario_id:<4} run {r.run_index}: {r.status.upper():<5} {r.duration_s:>5}s"
                  + (f"  {r.failed_checks}" if r.failed_checks else "") + (f"  ERROR {r.error}" if r.error else ""))

    summary = summarize(results, scenarios)
    print_matrix(summary)
    report = {
        "meta": {"eval_run_id": eval_run_id, "release": AGENT_RELEASE, "model": settings.model,
                 "runs_per_scenario": a.runs, "duration_s": round(time.perf_counter() - started),
                 "created_at": datetime.now(timezone.utc).isoformat()},
        "summary": summary,
        "results": [asdict(r) for r in sorted(results, key=lambda r: (r.scenario_id, r.run_index))],
    }
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"{eval_run_id}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report: {out}")
    if a.label:
        shutil.copy(out, REPORTS / f"{a.label}.json")
        print(f"saved as: {REPORTS / (a.label + '.json')}")
    if a.compare:
        print()
        if compare(Path(a.compare), out):
            sys.exit(1)


if __name__ == "__main__":
    main()
