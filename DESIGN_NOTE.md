# Design note

**Thesis.** Scheduling agents rarely fail on the happy path. They fail on the boring path: a timed-out booking gets
retried and double-books, or "you're confirmed" is said when nothing was saved. So I designed the failure
handling first, and let the evaluation harness measure whether the agent keeps those guarantees under real
LLM behaviour.

**The LLM proposes, code decides.** Gemini 2.5 Flash (LangGraph) holds the conversation and picks tools. It can only
call `SchedulingService`. Underneath, a state machine (13 states, transitions T01–T27 in `RELIABILITY_SPEC.md`)
rejects illegal moves: it won't book without an explicit "yes" to a held slot, and it won't choose a slot it never offered. The LLM
never writes state. Retries, timeouts and failure classification (F1–F10) live in code (`reliability/policy.py`),
not in the prompt, so every guarantee is testable without an LLM (50 pytest tests).

**Tool scoping.** The agent gets narrow, intent-level tools (`choose_slot`, `confirm_booking(patient_confirmed)`),
not raw CRUD. Every write carries an idempotency key. A write with an unknown outcome (a timeout, or a 5xx) is
**never retried blindly**: the system reads it back by key first (I7). Reschedules are make-before-break. If the old
booking can't be cancelled, both stay live and staff are notified, rather than silently undoing anything.

**Prompt guardrails.** Tool results are the only source of truth. Never claim BOOKED or "message sent" unless a tool
returned it. An ambiguous "sure, but…" is not consent. No medical advice. Emergencies go to emergency services.

**Evaluation.** 11 YAML scenarios. Each targets a failure class: response lost after commit, slot stolen, outage,
duplicates, identity switch mid-booking, hold expiry, failed cancel during a reschedule. Faults are injected on
the HTTP transport, so they look exactly like real ones. The checks are deterministic and read the **DB and backend,
not the transcript**: expected outcome, invariants, truthful claims (I3), no blind retry (I7).

**Where the rubric is blind.** A transcript-only judge can't see a double booking or a silent duplicate commit,
because the transcript looks perfect. That's why correctness is never LLM-judged. My checks have the opposite blind
spot: they test safety, not preference-following. In one S9 run "the latest afternoon slot" got 14:00, not 16:30,
and every check passed. A narrow LLM judge for wording and preferences is the right complement. It isn't built yet.
With 3 runs per scenario, a single flaky run moves a scenario by 33 points, so 33% and 67% mean "unstable", not a rate.

**Improvement loop.** Baseline 81.8% (33 runs), with zero guarantee violations. For each failure, the harness output gives the
first point of divergence, a classification (F#, I#, layer), **one** scoped change, a spec update, a regression test, a re-run and
`eval.compare` (exit 1 on regression). **IMP-001:** S9 was 0/3. The tool said the date window was inclusive, but the
backend treated the end date as exclusive, so "same day" was empty. That's a tool-contract bug, not a prompt bug. I fixed it in the
gateway. The guard test failed before and passes after. **S9 went from 0% to 100%.** S8 (prompt wording) and S11 (timestamps compared as
strings) are diagnosed in `eval/improvements/BACKLOG.md`. I didn't apply them, to keep each score movement
attributable to one change.

**Assumptions.** One appointment in flight per session. Identity is full name + date of birth (3 attempts, then
escalate). Hold TTL is 10 minutes. The backend is a mock (FastAPI + SQLite). All times are UTC.
