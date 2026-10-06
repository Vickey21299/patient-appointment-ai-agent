from eval.checks import TurnRecord, claims_in, no_blind_write_retry, truthfulness
from eval.scenario import load_all


def test_all_scenarios_load_and_validate():
    scs = load_all()
    assert len(scs) >= 10
    assert len({s.id for s in scs}) == len(scs)
    assert all(s.turns and s.expect for s in scs)


def test_claim_detection_and_negation():
    assert claims_in("Great! You're booked for Tuesday at 9:00.") == ["booked"]
    assert "notice_sent" in claims_in("A confirmation message has been sent to you.")
    assert claims_in("Your appointment is not booked yet.") == []
    assert claims_in("The confirmation message hasn't been sent yet, sorry.") == []


def _turn(reply, state="BOOKED", notice="SENT", active=1, events=()):
    return TurnRecord("u", reply, state, notice, active, None, None, list(events))


def test_truthfulness_flags_false_notice_claim():
    ok = truthfulness([_turn("You're booked. A confirmation message has been sent.")])
    bad = truthfulness([_turn("You're booked. A confirmation message has been sent.", notice="FAILED_RETRYING")])
    assert ok.passed and not bad.passed and "notice_sent" in bad.detail


def test_truthfulness_flags_booking_claim_without_booking():
    assert not truthfulness([_turn("You're all set, it's booked!", state="OFFERED", active=0)]).passed


def test_blind_retry_detected_and_reconciled_retry_allowed():
    blind = [{"name": "tool.call", "tool": "book_appointment"},
             {"name": "failure.classified", "tool": "book_appointment", "failure_class": "F2"},
             {"name": "tool.call", "tool": "book_appointment"}]
    safe = blind[:2] + [{"name": "reconcile.resolved"}] + blind[2:]
    notice = [{"name": "failure.classified", "tool": "send_notification", "failure_class": "F2"},
              {"name": "tool.call", "tool": "send_notification"}]
    assert not no_blind_write_retry(blind).passed
    assert no_blind_write_retry(safe).passed
    assert no_blind_write_retry(notice).passed   # I7 exemption: idempotent outbox resend
