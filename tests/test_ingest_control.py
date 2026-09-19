"""Active ingest cancellation bookkeeping."""

from __future__ import annotations

from autoclip import ingest_control


def test_active_ingests_can_be_cancelled_and_unregistered() -> None:
    first = ingest_control.register()
    second = ingest_control.register()
    try:
        assert ingest_control.active_count() == 2
        assert first.is_set() is False
        assert second.is_set() is False

        assert ingest_control.cancel_all() == 2
        assert first.is_set() is True
        assert second.is_set() is True
    finally:
        ingest_control.unregister(first)
        ingest_control.unregister(second)

    assert ingest_control.active_count() == 0
