"""Daily log files for following pipeline status.

logs/pipeline_YYYY-MM-DD.log   one file per day; files older than `retention_days` are deleted.
Each line: time | level | logger | trace/session/appointment | message.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"


class DailyFileHandler(logging.Handler):
    """Writes to <dir>/<prefix>_<YYYY-MM-DD>.log and switches file when the date changes.

    Opening a new file per day (instead of renaming at midnight) avoids the file-lock
    problems TimedRotatingFileHandler has on Windows.
    """

    def __init__(self, log_dir: str | Path, prefix: str = "pipeline", retention_days: int = 14):
        super().__init__()
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.prefix, self.retention_days = prefix, retention_days
        self._day: date | None = None
        self._stream = None

    def path_for(self, day: date) -> Path:
        return self.dir / f"{self.prefix}_{day.isoformat()}.log"

    def _roll(self, today: date) -> None:
        if self._stream:
            self._stream.close()
        self._stream = open(self.path_for(today), "a", encoding="utf-8")
        self._day = today
        self._purge(today)

    def _purge(self, today: date) -> None:
        cutoff = today - timedelta(days=self.retention_days)
        for f in self.dir.glob(f"{self.prefix}_*.log"):
            try:
                if date.fromisoformat(f.stem.split("_", 1)[1]) < cutoff:
                    f.unlink()
            except (ValueError, OSError):
                continue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            today = datetime.fromtimestamp(record.created).date()
            if today != self._day:
                self._roll(today)
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._stream:
            self._stream.close()
            self._stream = None
        super().close()


_configured = False
_lock = threading.Lock()   # eval runs set up runtimes in parallel threads


def setup_logging(log_dir: str | Path | None = None, level: str | None = None,
                  retention_days: int | None = None, console: bool = True) -> Path:
    """Idempotent. Returns the log directory. Env overrides: LOG_DIR, LOG_LEVEL, LOG_RETENTION_DAYS."""
    global _configured
    log_dir = Path(log_dir or os.getenv("LOG_DIR", "logs"))
    with _lock:
        if _configured:
            return log_dir
        _configure(log_dir, level, retention_days, console)
        _configured = True
    return log_dir


def _configure(log_dir: Path, level, retention_days, console) -> None:
    root = logging.getLogger()
    root.setLevel(level or os.getenv("LOG_LEVEL", "INFO"))
    fmt = logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    fh = DailyFileHandler(log_dir, retention_days=int(retention_days or os.getenv("LOG_RETENTION_DAYS", 14)))
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        ch.setLevel(os.getenv("LOG_CONSOLE_LEVEL", "WARNING"))   # console shows only problems; the file has everything
        root.addHandler(ch)
    for noisy in ("httpx", "httpx2", "httpcore", "urllib3", "google_genai", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.ERROR)   # repetitive AFC advisory on every call
