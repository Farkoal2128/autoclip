"""Track active source-ingest work so shutdown can cancel it cleanly."""

from __future__ import annotations

import threading

_lock = threading.Lock()
_active: set[threading.Event] = set()


def register() -> threading.Event:
    """Register one active ingest and return its cancellation event."""
    event = threading.Event()
    with _lock:
        _active.add(event)
    return event


def unregister(event: threading.Event) -> None:
    """Remove a completed ingest from the active set."""
    with _lock:
        _active.discard(event)


def active_count() -> int:
    """Return the number of currently active ingests."""
    with _lock:
        return len(_active)


def cancel_all() -> int:
    """Request cancellation for every active ingest and return how many were signalled."""
    with _lock:
        events = tuple(_active)

    for event in events:
        event.set()
    return len(events)
