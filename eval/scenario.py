"""YAML scenario loading and validation."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCENARIO_DIR = Path(__file__).with_name("scenarios")

EXPECT_KEYS = {
    "final_state", "final_state_any", "notice_status", "active_bookings", "patient_active_bookings",
    "tool_calls", "tool_calls_min", "tool_calls_max", "events_required", "events_forbidden",
    "escalation_reason", "final_reply_mentions_any",
}
TURN_KEYS = {"user", "request_id", "advance_minutes"}


@dataclass
class Turn:
    user: str
    request_id: str | None = None
    advance_minutes: float = 0


@dataclass
class Scenario:
    id: str
    title: str
    primary_failure_class: str
    why_it_matters: str
    turns: list[Turn]
    expect: dict[str, Any]
    faults: list[dict[str, Any]] = field(default_factory=list)
    setup: dict[str, Any] = field(default_factory=dict)
    exercises: dict[str, list[str]] = field(default_factory=dict)
    path: str = ""


def load(path: Path) -> Scenario:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    unknown = set(raw.get("expect", {})) - EXPECT_KEYS
    if unknown:
        raise ValueError(f"{path.name}: unknown expect keys {unknown}")
    turns = []
    for t in raw["turns"]:
        if set(t) - TURN_KEYS:
            raise ValueError(f"{path.name}: unknown turn keys {set(t) - TURN_KEYS}")
        turns.append(Turn(**t))
    return Scenario(
        id=raw["id"], title=raw["title"], primary_failure_class=str(raw["primary_failure_class"]),
        why_it_matters=raw.get("why_it_matters", ""), turns=turns, expect=raw.get("expect", {}),
        faults=raw.get("faults", []), setup=raw.get("setup", {}), exercises=raw.get("exercises", {}),
        path=str(path),
    )


def load_all(ids: list[str] | None = None) -> list[Scenario]:
    scenarios = [load(p) for p in sorted(SCENARIO_DIR.glob("*.yaml"))]
    if ids:
        wanted = {i.upper() for i in ids}
        scenarios = [s for s in scenarios if s.id.upper() in wanted]
    return scenarios
