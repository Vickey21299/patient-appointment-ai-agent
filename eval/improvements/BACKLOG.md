# Improvement backlog (diagnosed, not yet applied)

Each item below has a root cause taken from baseline `20261005-201333`, and each will go through the same loop as
[IMP-001](IMP-001-inclusive-search-window.md): one change, then a re-run, then `eval.compare`. They are kept
separate so each improvement can be attributed to its own change.

## IMP-002 (proposed): S8, re-asks for identity it was just given (baseline 33%, 1/3)

| | |
|---|---|
| Observed | Runs 2 and 3 end in `COLLECTING` with 0 bookings for p_003. After `switch_patient`, the agent asks "please provide Meera Iyer's full name and date of birth" on 4 consecutive turns, although the patient gave both in the same message that triggered the switch. `verify_patient` is never called |
| First divergence | Turn 4 reply, right after `switch_patient` returned `NEW_SESSION` |
| Root cause | Agent-facing wording. The prompt says "verify the new person **from scratch**" and the `switch_patient` docstring says "verify again". The model reads both as "ask again" rather than "do not reuse the previous person's identity" |
| Taxonomy | F8 (patient changes), I8 not violated (safe but stuck) · layer: **prompt** |
| Proposed change | `agent/prompts.py` "Changes" bullet and `agent/tools.py::switch_patient` docstring: *"Never reuse the previous person's details. If the patient already gave the new person's full name and date of birth, call verify_patient with them immediately. Ask only for what is missing."* |
| Predicted effect | S8 33% → 100%. Risk: none to safety, since `verify_patient` still checks the backend. Watch S1 and S7 for unprompted verification |
| Regression guard | S8 itself. A prompt change can't be unit-tested without an LLM, so the YAML scenario is the guard |

## IMP-003 (proposed): S11, timestamp-format mismatch on choose_slot (baseline 67%, 2/3)

| | |
|---|---|
| Observed | Run 2 fails `calls-hold_slot-min` (1, want ≥ 2) |
| First divergence | Turn 2: `choose_slot(slot_start="2026-10-07T09:00:00Z")` is REJECTED with `slot_not_offered`. The offered value was `2026-10-07T09:00:00+00:00`, which is the same instant in a different notation (Langfuse trace `9bcb58f6…`) |
| Consequence | The F9 guard worked: nothing was booked wrongly. But the hold only happened after the 11-minute clock jump, so the expiry path the scenario targets (T09 → re-hold → re-confirm) was never exercised |
| Root cause | `SchedulingService.choose_slot` compares offered slots by **string**, not by instant |
| Taxonomy | F9 guard false positive · layer: **state-guard** |
| Proposed change | `reliability/service.py::choose_slot` (and `reschedule_choose`): match on `parse(slot_start) == parse(offered.slot_start)`, then use the offered canonical string from then on |
| Predicted effect | S11 67% → 100%. This is deterministic, so a pytest guard is possible: `choose_slot` with a `Z`-suffixed timestamp returns `HELD` |
| Regression guard | New unit test, plus S8's hallucinated-slot test must still reject a genuinely different instant |

## Rubric gap found during IMP-001 verification

- **S9 slot preference is not checked.** The patient asked for "the latest afternoon slot" and the agent picked 14:00, not 16:30. All checks still pass, because S9 asserts the safety outcome only. Proposed: an optional `expect.chosen_slot` check in `eval/checks.py`. This is exactly the kind of preference-following a transcript judge *can* see, while the DB checks can't.
