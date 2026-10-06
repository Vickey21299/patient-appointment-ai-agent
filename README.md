# Appointment Scheduling Agent: reliability first, with a closed improvement loop

A patient-appointment scheduling agent (LangGraph + Gemini 2.5 Flash) on top of a deterministic reliability
layer, plus a scenario-based evaluation harness. The harness flagged a real failure. It was turned into one structured
improvement and re-run: **S9 went from 0% to 100%, with no regressions.**

- 🎥 **Recording:** <!-- TODO: paste Loom link --> _link_
- 📄 **Design note (1 page):** [DESIGN_NOTE.md](DESIGN_NOTE.md)

## Run it

Setup (once): Python 3.11+, then copy `.env.example` to `.env` and set `GEMINI_API_KEY` (Langfuse keys are optional).

```
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"      # macOS/Linux: .venv/bin/pip
```

**Run the agent** (chat in the terminal):

```
.venv/Scripts/python -m agent.cli
```

**Run the eval loop** (all 11 scenarios × 3 runs against Gemini, compared to the baseline, exits 1 on any regression):

```
.venv/Scripts/python -m eval.run -n 3 --compare eval/reports/baseline.json
```

<details><summary>More commands</summary>

```
.venv/Scripts/python -m pytest -q                                               # 50 deterministic tests, no LLM, ~7 s
.venv/Scripts/python -m uvicorn server.app:create_server --factory --port 8000  # web UI with live state/events: http://localhost:8000
.venv/Scripts/python -m agent.cli --demo --fault book_appointment:timeout_after_commit:1   # lost-response case in the terminal
.venv/Scripts/python -m eval.run -s S9 -n 3 --compare eval/reports/baseline.json           # one scenario
.venv/Scripts/python -m eval.compare eval/reports/baseline.json eval/reports/after_imp001.json
```
</details>

## Results

**Baseline** (`20261005-201333`, 11 scenarios × 3 runs against real Gemini): **81.8%**. Across all 33 runs: **0 double
bookings, 0 false claims to the patient, 0 blind write retries.**

**The loop, closed once (IMP-001)**

| Step | Evidence |
|---|---|
| A run | [eval/reports/baseline.json](eval/reports/baseline.json): S9 "reschedule where cancel fails" **0/3** |
| The eval flags the failure | `final-state` got MODIFYING (want ESCALATED); the agent said "no slots" while the DB had 11 free |
| Diagnosis | Tool-contract skew. `date_to` was described as inclusive, but the backend treated it as exclusive, so "same day" was an empty window. [IMP-001](eval/improvements/IMP-001-inclusive-search-window.md) |
| The improvement applied | One scoped change in [gateway/tools.py](gateway/tools.py), a spec note in the same change, and a regression test that **failed before** the fix and passes after |
| The re-run shows the score move | [compare_baseline_vs_after_imp001.txt](eval/reports/compare_baseline_vs_after_imp001.txt): **S9 0% → 100%, FIXED** · deterministic suite 50/50 |

Diagnosed and queued for the same loop: [eval/improvements/BACKLOG.md](eval/improvements/BACKLOG.md). That covers S8 (prompt wording, 33%),
S11 (timestamps compared as strings, 67%) and a rubric gap (slot preference isn't checked).

## Where to look

| | |
|---|---|
| Agent, prompt, tools | [agent/prompts.py](agent/prompts.py) · [agent/tools.py](agent/tools.py) · `agent/graph.py` |
| Behavioural contract | [RELIABILITY_SPEC.md](RELIABILITY_SPEC.md): states, T01–T27, failure classes F1–F10, invariants I1–I12 |
| Architecture | [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) |
| Scenarios and checks | [eval/scenarios/](eval/scenarios/) · [eval/checks.py](eval/checks.py) · [eval/invariants.py](eval/invariants.py) |
| Improvement records | [eval/improvements/](eval/improvements/) |
| Tracing | Langfuse: one trace per patient turn, one session per conversation, PII masked ([observability/](observability/)) |

## How AI was used, and where my judgment overrode it

**Where AI helped**
- **Claude Code** was the main pair-programmer. It drafted the reliability spec, the reliability layer, the
  scenarios and checks, the UI and these docs. It also ran the IMP-001 diagnosis: it traced the S9 failure to the
  date-window contract and wrote the regression test first.
- I set the constraints it worked under ([CLAUDE.md](CLAUDE.md)):
  - a locked stack
  - "don't build our own tracing or agent framework"
  - the LLM never writes state
  - never retry a write whose outcome is unknown
  - one root cause per improvement
  - cite spec IDs in code
- I defined review sub-agents for it in `.claude/agents/`: `failure-analyst`, `spec-guardian` and `scenario-author`.
- **Gemini 2.5 Flash** is the runtime agent under test.

**Where my judgment overrode it**
- **Scope of the loop.** The AI proposed a second improvement on S8 and a full 33-run re-measure. I chose to close the
  loop on S9 only, measured with a targeted S9 re-run. I kept S8 and S11 as diagnosed backlog, so the one score movement is
  cleanly attributable and Gemini cost stays proportionate.
- **Stopping a run.** I stopped an in-flight full-matrix re-run that the AI had launched, and asked for the cheapest proof
  that still answers the question.
- <!-- TODO: add your own overrides here, e.g. a design choice, prompt rule or spec decision where you rejected the AI's suggestion -->

## Assumptions

- **One appointment in flight** per patient session.
- **Identity** is full name + date of birth. After 3 failed attempts the conversation escalates. Switching to another person discards identity, holds and collected details.
- **Consent:** a booking needs an explicit, in-conversation "yes" to a named slot.
- **Hold TTL is 10 minutes.** An expired hold is renewed and the patient is asked again, never booked silently.
- **Reschedule is make-before-break.** If the old booking can't be cancelled, both bookings stay live and staff are notified.
- **5xx on a write counts as an unknown outcome:** it is read back before any retry.
- **The backend is a mock** (FastAPI + SQLite) standing in for the clinic system. All times are UTC. No medical advice is given.
