import logging
from datetime import date, timedelta

from observability.langfuse_tracing import mask_pii
from observability.logging_setup import DailyFileHandler


def test_mask_pii_redacts_dob_phone_email_keys_but_keeps_slot_dates():
    data = {"msg": "I'm Asha, born 1990-04-12, call +91-90000-00001 or asha@example.com",
            "dob": "1990-04-12", "slot_start": "2026-10-07T09:00:00+00:00", "date_from": "2026-10-07",
            "nested": ["key sk-lf-00000000-0000-0000"]}
    out = mask_pii(data=data)
    assert "1990-04-12" not in str(out) and "90000" not in str(out) and "asha@example.com" not in str(out)
    assert out["dob"] == "[REDACTED_DOB]" and "sk-lf-" not in str(out["nested"])
    assert out["slot_start"] == "2026-10-07T09:00:00+00:00" and out["date_from"] == "2026-10-07"


def test_daily_handler_writes_dated_file_and_purges_old(tmp_path):
    old = tmp_path / f"pipeline_{(date.today() - timedelta(days=30)).isoformat()}.log"
    old.write_text("old")
    h = DailyFileHandler(tmp_path, retention_days=14)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.emit(logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None))
    h.close()
    today = tmp_path / f"pipeline_{date.today().isoformat()}.log"
    assert today.read_text(encoding="utf-8").strip() == "hello"
    assert not old.exists()
