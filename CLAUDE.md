# Appointment Scheduling Agent: project guide

A reliability-first appointment scheduling agent. What sets it apart is the chain
**failure taxonomy → retry policy → recovery → observable traces → scenario evaluation → structured improvement → regression proof**.

## Source-of-truth documents

| File | Role | Rule |
|---|---|---|
| `RELIABILITY_SPEC.md` | Behavioural contract: states, transitions (T01–T27), failure classes (F1–F10), invariants (I1–I12) | Code must match it. If code needs to differ, update the spec **in the same change** |
| `SYSTEM_DESIGN.md` | Architecture: components, boundaries, diagrams | Don't copy spec rules into it. Reference them by ID |

Cite spec IDs in code comments and tests (e.g. `# T13, I7`) wherever a rule is enforced.

## Stack (locked)

LangGraph (dialogue) · FastAPI (mock backend) · SQLite (state) · Langfuse (tracing) · pytest + YAML (evaluation) · LLM judge only where deterministic assertions can't decide.
Do **not** build our own tracing framework, agent framework or a large UI.

## Layering (enforced)

```
agent/  →  reliability/  →  gateway/  →  backend/
                 ↘ storage/ ↙
```

- `reliability/` must never import `agent/`. All guarantees must be testable without an LLM.
- `reliability/clock.py` is a dependency-free leaf that any layer may import (the backend uses it for hold TTLs).
- The agent calls only `reliability/service.py::SchedulingService`. It never calls the gateway or backend directly.
- Fault injection (`backend/injector.py`) sits on the HTTP transport between gateway and backend, so injected faults look exactly like real ones.
- The LLM proposes events. `reliability/state_machine.py` validates them. The LLM never writes state.
- Retry, timeout and classification logic lives only in `reliability/policy.py` and `reliability/executor.py`. Never put it in prompts.
- Never retry a write whose outcome is unknown. Reconcile first (`reliability/reconciler.py`, I7).

## Commands

```
.venv/Scripts/python -m pytest -q                 # all tests
.venv/Scripts/python -m pytest tests/test_scenarios_core.py -q
.venv/Scripts/python -m agent.cli                         # chat with the Gemini agent
.venv/Scripts/python -m agent.cli --demo --fault book_appointment:timeout_after_commit:1
.venv/Scripts/python -m observability.status             # pipeline status + today's log tail
.venv/Scripts/python scripts/inspect_trace.py <trace_id>  # Langfuse trace tree (v2 observations API)
.venv/Scripts/python -m uvicorn server.app:create_server --factory --port 8000   # web UI (frontend/) at http://localhost:8000
.venv/Scripts/python -m eval.run -n 3                     # scenario matrix vs real Gemini agent
.venv/Scripts/python -m eval.run -s S3 S9 -n 1            # subset
.venv/Scripts/python -m eval.compare eval/reports/baseline.json eval/reports/<run>.json   # regression proof
```

## Evaluation

- Scenarios: `eval/scenarios/*.yaml`. Fault call numbers count from the moment the fault is armed.
- Every run uses a fresh DB, a `FakeClock` and its own Langfuse session (`eval-<run>-<scenario>-r<n>`, environment `evaluation`).
- Checks are deterministic (`eval/checks.py`): expected-outcome, invariants-hold, truthful-claims (I3), no-blind-write-retry (I7). Each becomes a session score in Langfuse.
- A run with status `error` means an infrastructure error (e.g. a Gemini API error). It is excluded from the pass rate and reported separately.
- The improvement loop: run baseline → `failure-analyst` → one change → re-run → `eval.compare`. The baseline is `eval/reports/baseline.json`.

## Observability

- **Langfuse** (`observability/langfuse_tracing.py`): one trace per patient turn (`patient-turn`), with all turns grouped by `session_id`; `user_id` = patient_id after verification. LangGraph and Gemini are traced by the LangChain `CallbackHandler`. Backend calls are `tool` observations, reconciliation is a span, and reliability events (§7) are `event` observations. PII (DOB, phone, email, keys) is masked by `mask_pii`.
- This project is on Langfuse v4: read traces via `GET /api/public/v2/observations`. The legacy `/api/public/traces` returns 410.
- **Logs**: `logs/pipeline_YYYY-MM-DD.log`, one file per day, kept for 14 days. The console shows WARNING and above only.
- The Langfuse skill is installed at `.claude/skills/langfuse`. Fetch current docs before changing instrumentation.

## Conventions

- Times are timezone-aware UTC, stored as ISO-8601 strings. Always read "now" from the injected `Clock`, never from `datetime.now()`, so tests can time-warp (I5).
- Every state change goes through `StateMachine.apply()`. That writes a `transitions` row and emits a `state.transition` event (I10).
- Event names follow `RELIABILITY_SPEC.md` §7.
- One root cause per improvement change (see `.claude/agents/failure-analyst.md`).

## Agent team (`.claude/agents/`)

| Agent | Use it to |
|---|---|
| `reliability-engineer` | Implement or change code in `reliability/`, `gateway/`, `backend/`, `storage/` against the spec |
| `scenario-author` | Write YAML evaluation scenarios in `eval/scenarios/` |
| `failure-analyst` | Diagnose a failing scenario or test and propose one scoped fix |
| `spec-guardian` | Review a change for spec, invariant and layering violations before it is accepted |
