"""Process-local graceful server shutdown control."""

from __future__ import annotations

from autoclip import server_control


def test_shutdown_callback_can_be_registered_and_cleared() -> None:
    calls: list[str] = []

    server_control.register_shutdown(lambda: calls.append("quit"))
    try:
        assert server_control.shutdown_available() is True
        assert server_control.request_shutdown() is True
        assert calls == ["quit"]
    finally:
        server_control.clear_shutdown()

    assert server_control.shutdown_available() is False
    assert server_control.request_shutdown() is False
