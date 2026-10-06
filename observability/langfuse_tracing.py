"""Langfuse adapter (SDK v4, OpenTelemetry-based).

- init_langfuse(): one client per process, created AFTER .env is loaded, with PII masking,
  environment and release set.
- LangfuseTracer: implements reliability.events.Tracer with typed observations.
- LangfuseSink: turns reliability events into `event` observations nested in the active span.

If keys are missing, tracing is disabled and everything still runs (logs only).
"""
from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger("observability")

AGENT_RELEASE = "0.2.1"  # 0.2.1: IMP-001 inclusive search window

# DOB-like: a date in 1900-2019 not followed by a time. Appointment/search dates (2026+) stay visible.
_DOB = re.compile(r"\b(19\d{2}|20[01]\d)-\d{2}-\d{2}\b(?!T)")
_PHONE_CANDIDATE = re.compile(r"\+?\d[\d\-\s]{8,}\d")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_SECRET = re.compile(r"\b(sk-lf-|pk-lf-|AIza|AQ\.)[\w\-.]{10,}")


def _phone(m: re.Match) -> str:
    s = m.group(0)
    if _ISO_DATE.search(s) or sum(ch.isdigit() for ch in s) < 10:
        return s
    return "[REDACTED_PHONE]"


def mask_pii(*, data: Any, **kwargs: Any) -> Any:
    """Langfuse mask function: redacts DOBs, phone numbers, emails and API keys everywhere."""
    if isinstance(data, str):
        data = _SECRET.sub("[REDACTED_KEY]", data)
        data = _EMAIL.sub("[REDACTED_EMAIL]", data)
        data = _DOB.sub("[REDACTED_DOB]", data)
        return _PHONE_CANDIDATE.sub(_phone, data)
    if isinstance(data, dict):
        return {k: ("[REDACTED_DOB]" if k == "dob" else mask_pii(data=v)) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask_pii(data=v) for v in data]
    return data


_client = None


def init_langfuse():
    """Returns the Langfuse client, or None when keys are absent or LANGFUSE_TRACING_ENABLED=false."""
    global _client
    if _client is not None:
        return _client
    if os.getenv("LANGFUSE_TRACING_ENABLED", "true").lower() == "false" or not (
            os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        log.warning("Langfuse tracing disabled (no keys or LANGFUSE_TRACING_ENABLED=false)")
        return None
    from langfuse import Langfuse

    _client = Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        base_url=os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST"),
        environment=os.getenv("LANGFUSE_TRACING_ENVIRONMENT", "development"),
        release=AGENT_RELEASE,
        mask=mask_pii,
    )
    log.info("Langfuse tracing enabled -> %s (env=%s)", os.getenv("LANGFUSE_BASE_URL"),
             os.getenv("LANGFUSE_TRACING_ENVIRONMENT", "development"))
    return _client


class LangfuseTracer:
    def __init__(self, client):
        self.client = client

    @contextmanager
    def observe(self, name: str, *, as_type: str = "span", input: Any = None,
                metadata: dict[str, Any] | None = None) -> Iterator[Any]:
        with self.client.start_as_current_observation(name=name, as_type=as_type, input=input,
                                                      metadata=metadata) as obs:
            yield obs


_LEVELS = {
    "escalation.packet": "ERROR", "budget.exhausted": "ERROR", "circuit.open": "ERROR",
    "failure.classified": "WARNING", "retry.scheduled": "WARNING", "transition.rejected": "WARNING",
    "conflict.detected": "WARNING", "duplicate.detected": "WARNING", "compensation.run": "WARNING",
    "orphan_hold": "WARNING", "reconcile.start": "WARNING",
}
# tool.call is already represented by the `tool` observation the executor opens; skip the duplicate.
_SKIP = {"tool.call"}


class LangfuseSink:
    """Each reliability event becomes an `event` observation (name: dots -> hyphens, e.g. state-transition)."""

    def __init__(self, client):
        self.client = client

    def emit(self, name: str, **attrs: Any) -> None:
        if name in _SKIP:
            return
        meta = {k: v for k, v in attrs.items() if v is not None and k != "trace_id"}
        level = _LEVELS.get(name, "DEFAULT")
        if name == "saga.step" and attrs.get("status") not in ("ok", "SENT"):
            level = "WARNING"
        kwargs: dict[str, Any] = {"name": name.replace(".", "-").replace("_", "-"), "metadata": meta, "level": level}
        if name == "state.transition":
            kwargs["input"] = {"from": attrs.get("from_state"), "event": attrs.get("event")}
            kwargs["output"] = {"to": attrs.get("to_state"), "transition": attrs.get("transition")}
        if level != "DEFAULT":
            kwargs["status_message"] = str(attrs.get("reason") or attrs.get("failure_class") or name)
        try:
            self.client.create_event(**kwargs)
        except Exception:  # tracing must never break the pipeline
            log.exception("failed to emit Langfuse event %s", name)
