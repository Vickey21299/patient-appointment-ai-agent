"""Observability events (RELIABILITY_SPEC §7). Langfuse is plugged in as one sink; tests use ListSink."""
from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from typing import Any, Iterator, Protocol


class EventSink(Protocol):
    def emit(self, name: str, **attrs: Any) -> None: ...


class ObservationHandle(Protocol):
    def update(self, **kwargs: Any) -> Any: ...


class Tracer(Protocol):
    """Opens a nested observation (span/tool) around a unit of work. Langfuse implements it in
    observability/langfuse_tracing.py; the reliability layer depends only on this protocol."""

    def observe(self, name: str, *, as_type: str = "span", input: Any = None,
                metadata: dict[str, Any] | None = None) -> AbstractContextManager[ObservationHandle]: ...


class _NullHandle:
    def update(self, **kwargs: Any) -> None:
        pass


class NullTracer:
    @contextmanager
    def observe(self, name: str, *, as_type: str = "span", input: Any = None,
                metadata: dict[str, Any] | None = None) -> Iterator[ObservationHandle]:
        yield _NullHandle()


class NullSink:
    def emit(self, name: str, **attrs: Any) -> None:
        pass


class ListSink:
    """Records every event in memory. Used by tests and by the eval harness for assertions."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, name: str, **attrs: Any) -> None:
        self.events.append({"name": name, **attrs})

    def names(self) -> list[str]:
        return [e["name"] for e in self.events]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["name"] == name]


class FanoutSink:
    def __init__(self, *sinks: EventSink) -> None:
        self.sinks = sinks

    def emit(self, name: str, **attrs: Any) -> None:
        for s in self.sinks:
            s.emit(name, **attrs)
