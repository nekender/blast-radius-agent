"""A tiny in-process event bus, so a UI can watch a run live.

The pipeline, the LLM ledger and the agents call `emit(...)`. Nothing is listening during CLI runs, so it costs
nothing there. The UI server attaches a sink (a thread-safe queue) for the duration of one run and streams what
arrives to the browser over Server-Sent Events.
"""
from __future__ import annotations

import queue
import threading
import time

_lock = threading.Lock()
_sink: queue.Queue | None = None


def attach(q: queue.Queue) -> None:
    global _sink
    with _lock:
        _sink = q


def detach() -> None:
    global _sink
    with _lock:
        _sink = None


def emit(event: str, /, **data) -> None:
    """`event` is positional-only, so payloads may carry their own `kind` field (ledger entries do)."""
    sink = _sink
    if sink is not None:
        sink.put({**data, "type": event, "t": round(time.time(), 3)})
