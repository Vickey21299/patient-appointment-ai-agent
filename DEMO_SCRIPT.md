# Demo video script (final, ~10 min)

The story: **design the reliability contract → the agent handles hard cases → the harness finds a real failure → one structured fix → proven 0% → 100% with no regressions.**

## Before recording (10 min)

1. Run the tests once (free, no LLM): `.venv/Scripts/python -m pytest -q` should show **50 passed**.
2. Start the UI: `.venv/Scripts/python -m uvicorn server.app:create_server --factory --port 8000`
3. Open http://localhost:8000. The top bar should show **LIVE · gemini-2.5-flash · Langfuse on**.
4. Second browser tab: Langfuse → your project → **Sessions**.
5. VS Code tabs, in this order:
   1. `README.md`
   2. `RELIABILITY_SPEC.md` (state diagram)
   3. `SYSTEM_DESIGN.md` (diagram 1)
   4. `agent/prompts.py`
   5. `eval/scenarios/S03_book_response_lost.yaml`
   6. `eval/improvements/IMP-001-inclusive-search-window.md`
   7. `gateway/tools.py` (the `search_slots` fix)
   8. `tests/test_scenarios_core.py` (scroll to the last test)
   9. `eval/reports/compare_baseline_vs_after_imp001.txt`
   10. `eval/improvements/BACKLOG.md`
6. Rehearse S1, S3, S6, S10 and S9 once each. The wording changes between runs. The states and events don't.

---

## 1. Problem and approach (1 min) · `README.md`, `RELIABILITY_SPEC.md`, `SYSTEM_DESIGN.md`

> "Booking agents rarely fail on the happy path. They fail in boring, expensive ways: a timeout gets retried and double-books, or the agent says 'confirmed' when nothing was saved."

> "So I wrote the reliability contract first: 13 states, failure classes F1 to F10, and invariants I1 to I12. Every rule in the code cites one of these IDs."

Show the state diagram in `RELIABILITY_SPEC.md`, then diagram 1 in `SYSTEM_DESIGN.md`.

> "The key design choice: **the LLM proposes and a deterministic layer decides.** Gemini can only call a service. A state machine rejects illegal moves, and the LLM never writes state."

## 2. Agent design (1 min) · `agent/prompts.py`

Point at the guardrails:
- Tools are the only source of truth.
- Never claim "booked" unless a tool returned BOOKED.
- Book only after an explicit yes to *that* slot ("Sure, but…" is not a yes).
- No invented slots.
- Emergencies go to emergency services.

> "The prompt holds the conversational rules. Retries, timeouts and idempotency are deliberately **not** in the prompt. They live in code, where they can be tested without an LLM."

## 3. Live use cases (4 min) · UI

UI tour (15 s): use cases with their baseline pass rate on the left, chat in the middle, live state, checks, bookings and events on the right.

For each case, click it, click the suggestion buttons in order, then click **Run checks**.

| Case | Say / point at |
|---|---|
| **S1** Happy path (30 s) | The progress bar fills to BOOKED. The events show each transition with its spec ID (T01, T02…). Notice `SENT`. All checks ✓ |
| **S3** ⭐ Booking saved, response lost (1 min) | Faults armed: `book_appointment · timeout_after_commit`. On "yes": `failure F2` → `reconcile: outcome unknown` → `reconcile → applied` → BOOKED. **Backend bookings: exactly one.** "A naive agent retries here and double-books. This one reads back by idempotency key first. Never retry a write whose outcome is unknown." |
| **S6** Notice fails (45 s) | State BOOKED, notice `FAILED_RETRYING`. The agent says the booking is confirmed but the message is *delayed*, not "sent". "Truthfulness is checked against the DB (I3), not trusted from the transcript." |
| **S10** Backend down (45 s) | Retries → `circuit open` → `escalated to staff`. "It stops hammering a dead service and tells the patient the truth: staff will follow up." |

> "Other hard cases are in the matrix too: slot stolen, duplicate messages, patient changes identity mid-booking, hold expiry. Eleven scenarios in total."

## 4. Evaluation harness (1.5 min) · `S03_book_response_lost.yaml`, `README.md`

Show the YAML: the patient turns, the injected fault, the expected outcome.

> "Every scenario runs against real Gemini, with a fresh DB, a simulated clock and its own Langfuse session. The checks are **deterministic**: final state, booking count, invariants, truthful claims, no blind retry. They read the database and the backend, not the transcript."

> "**Baseline: 11 scenarios × 3 runs = 33 runs, 81.8%.** Across all 33: zero double bookings, zero false claims, zero blind retries. The guarantees held. The failures were all elsewhere."

**Where the rubric is blind** (README section): say this, it's a strength.

> "A transcript-only judge can't see a double booking or a silent duplicate commit, because the transcript looks perfect. That's why correctness is never LLM-judged here. But my checks have a blind spot too: they check safety, not preference. In one S9 run the patient asked for the *latest* afternoon slot and got 2 PM instead of 4:30. Every check passed. That's where a narrow LLM judge would add value."

## 5. Closing the loop: IMP-001 (2.5 min) ⭐

Open `IMP-001-inclusive-search-window.md` and walk through it top to bottom:

1. **Failure:** "S9, reschedule where the cancel fails, scored **0 out of 3**."
2. **First divergence:** "On turn 1 the search returned zero slots, but the DB had 11 free slots that day."
3. **Root cause:** "The tool told the agent the dates were inclusive. The backend treated the end date as exclusive. 'Same day' became an empty window. The LLM did exactly what it was told, so this is a **tool-contract** bug, not a prompt bug."
4. **One fix:** show `gateway/tools.py` `search_slots`. "One scoped change, and the spec was updated in the same change."
5. **Regression guard:** show the last test in `test_scenarios_core.py`. "It **failed before** the fix and passes after, with no LLM needed. This bug can't silently come back."
6. **Re-run and compare:** show `compare_baseline_vs_after_imp001.txt`:
   ```
   S9    0%   100%   +100%  FIXED
   ```
   "Same harness, same model, 3 runs: **0% → 100%**. The full test suite still passes 50/50."

**Prove it live: run S9 in the UI.**
- It offers later slots, holds one, and on "yes" books the new one.
- `cancel_appointment` fails 3×, and each attempt is reconciled by key before retrying.
- The run ends `ESCALATED · DUPLICATE_LIVE`. **Two** live bookings, on purpose: "Make-before-break. It never silently undoes a booking. Staff resolve it, and the agent doesn't claim 'rescheduled'."
- Run checks: all ✓. "The left panel still shows 0%. That's the baseline, my 'before'."

Open `BACKLOG.md`:

> "The other two failures are already diagnosed and queued for the same loop: S8 is prompt wording (IMP-002), and S11 compares slot times as strings instead of instants (IMP-003). I found that one in the Langfuse trace. One change per iteration, so every score movement can be attributed."

## 6. Observability (45 s) · Langfuse tab

- In S3, click **trace ↗**. Show the tree: `confirm_booking` → **`book-appointment` (ERROR, F2)** → **`reconcile-write`** → state transitions.
- **Sessions** view: one session per conversation, one trace per message.
- PII masking: the date of birth shows as `[REDACTED_DOB]`.

## 7. Close (30 s)

> "To recap: a reliability contract first, an LLM that proposes but never decides, a harness that probes the failure modes and states its own blind spots, and an improvement loop that closed: S9 from 0 to 100% with no regressions."

> "Next: IMP-002 and IMP-003 through the same loop, a slot-preference check, and a CI gate. `eval.compare` already exits non-zero on any regression."

Stack: LangGraph · Gemini 2.5 Flash · FastAPI · SQLite · Langfuse · pytest + YAML.

---

### If something goes off-script

- **Different wording:** fine. Point at the **state and events**, not the text.
- **Infra error badge (Gemini API):** click **New conversation** and redo the case.
- **"Server unreachable":** restart uvicorn.
- **Asked about S8 or S11 live:** they're in the backlog with diagnosed root causes. Don't run them on camera, because they're the known flaky ones.
