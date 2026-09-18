"""Process-local control hook for gracefully stopping the AutoClip server."""

from __future__ import annotations

import threading
from collections.abc import Callable

_lock = threading.Lock()
_shutdown_callback: Callable[[], None] | None = None


def register_shutdown(callback: Callable[[], None]) -> None:
    """Register the callback that tells the active server to exit."""
    global _shutdown_callback
    with _lock:
        _shutdown_callback = callback


def clear_shutdown() -> None:
    """Remove the active-server shutdown callback."""
    global _shutdown_callback
    with _lock:
        _shutdown_callback = None


def shutdown_available() -> bool:
    with _lock:
        return _shutdown_callback is not None


def request_shutdown() -> bool:
    """Request a graceful server exit.

    Returns False when AutoClip was not started through the controllable server
    path (for example a development reload process).
    """
    with _lock:
        callback = _shutdown_callback

    if callback is None:
        return False

    callback()
    return True
