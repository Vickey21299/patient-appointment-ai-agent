"""Settings. Importing this module loads .env first, so Langfuse and Gemini see their keys."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    temperature: float = float(os.getenv("GEMINI_TEMPERATURE", "0"))
    thinking_budget: int = int(os.getenv("GEMINI_THINKING_BUDGET", "1024"))
    db_path: str = os.getenv("APP_DB", str(ROOT / "app.db"))
    log_dir: str = os.getenv("LOG_DIR", str(ROOT / "logs"))
    recursion_limit: int = int(os.getenv("AGENT_RECURSION_LIMIT", "25"))


settings = Settings()
