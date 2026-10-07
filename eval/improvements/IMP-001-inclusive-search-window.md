# IMP-001: inclusive single-day search window (fixes S9)

| | |
|---|---|
| Trigger | Baseline ([20261005-201333](../../reports/20261005-201333.json)): **S9 0% (0/3)**, overall 81.8% |
| Taxonomy | Not an F-class failure. Contract skew made T03 `no_slots` fire wrongly · layer: **tool-contract** |
| Release | 0.2.0 → 0.2.1 |

## Observed
S9 fails in every run with the same four checks: `final-state` (got MODIFYING, want ESCALATED),
`active-bookings` (1, want 2), `escalation-reason`, `final-reply-discloses`. The agent tells the
patient "no later slots with Dr. Rao on October 7th". The run DB, however, shows 11 free `dr_rao`
slots that day (`eval/.runs/20261005-201333/S9_r1.db`).

## First divergence
Turn 1, `search_slots` → HTTP 200 with `slots: []`. The patient asked for "the same day", so the
agent sent `date_from = date_to = 2026-10-07`. The backend filters `slot_start >= date_from AND
slot_start < date_to` ([backend/app.py](../../backend/app.py)), which is an empty range. The scenario's
fault (`cancel_appointment` 503) is never reached, because the reschedule never gets past the search.

## Root cause (one)
The tool contract says `date_from/date_to` are ISO dates "bounding the search window", which reads as an
inclusive calendar-date range. The backend treats `date_to` as an exclusive timestamp. The LLM did
what the contract said. The contract and the backend disagreed.

## Change (one, scoped)
- `gateway/tools.py::search_slots`: a date-only `date_to` is converted to `date_to + 1 day` before it
  is sent, so both bounds are inclusive. The backend stays unchanged (it stands in for the real system).
- `agent/tools.py`: the docstring now says "both inclusive… for one day, pass the same date for both".
- `RELIABILITY_SPEC.md` §2: a "Search window contract (IMP-001)" note, added in the same change.

The prompt is unchanged, so this cannot affect the agent's behaviour in other scenarios.

## Regression guard
`tests/test_scenarios_core.py::test_single_day_search_window_is_inclusive`. It drives
`SchedulingService` with no LLM:
- **before the fix: FAILED** (`'NO_SLOTS' == 'SLOTS'`)
- **after the fix: passed**. The full deterministic suite is **50/50 passed** (it was 49 before the new test).

## Predicted effect
- S9: 0% → 100%. The reschedule now finds slots, the injected cancel failure triggers T21, and the
  appointment escalates with `DUPLICATE_LIVE` and 2 live bookings.
- Possible regressions: only scenarios that pass a date-only `date_to`. The window is now wider, never
  narrower, so no previously found slot can disappear.

## Before / after (Gemini eval): loop closed ✅

Same harness, same scenario, same model (`gemini-2.5-flash`), 3 runs each:

```
.venv/Scripts/python -m eval.run -s S9 -n 3 --label after_imp001
.venv/Scripts/python -m eval.compare eval/reports/baseline.json eval/reports/after_imp001.json
```

| Scenario | Before: `20261005-201333` (0.2.0) | After: `20261006-154045` (0.2.1) | Verdict |
|---|---|---|---|
| **S9** | **0%** (FAIL FAIL FAIL) | **100%** (PASS PASS PASS) | **FIXED** |

Full output: [compare_baseline_vs_after_imp001.txt](../reports/compare_baseline_vs_after_imp001.txt).
(The compare's "overall" line covers only S9 here, because only S9 was re-run.)

**No-regression evidence**
- The deterministic suite passes 50/50, including the new guard, which **failed before** the fix and passes after.
- The change only widens a date-only `date_to` by one day. It can add slots to a search result, never remove them.
- The prompt and the state machine are unchanged, so the other scenarios' agent behaviour is unaffected.
- Not done: a full 11-scenario Gemini re-run. That's planned together with IMP-002 and IMP-003 ([BACKLOG.md](BACKLOG.md)).

Manual check in the UI: session `ui-s9-643fd8a1` passed all 11 checks: `ESCALATED / DUPLICATE_LIVE`, 2 live bookings,
3 cancel attempts each reconciled by key before retrying, and an honest "staff will follow up" reply.

## New finding from the after run (for a later IMP, not part of this change)
The patient asked for "the latest afternoon slot" and the agent picked 2:00 PM (the latest was 4:30 PM).
S9 still passes, because its checks assert the safety outcome, not the patient's slot preference. That is a
coverage gap in the rubric. Candidate fix: an `expect.chosen_slot` check in `eval/checks.py`.
