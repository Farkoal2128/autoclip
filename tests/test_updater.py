"""Unit tests for the detached self-update helper."""

from __future__ import annotations

import pytest
from autoclip import updater


def test_update_result_round_trip(autoclip_home) -> None:
    updater._write_result(
        "token",
        status="success",
        message="Updated",
        from_revision="old",
        to_revision="new",
    )

    result = updater.read_result("token")
    assert result is not None
    assert result["status"] == "success"
    assert result["from_revision"] == "old"
    assert result["to_revision"] == "new"
    assert updater.read_result("other") is None

    updater.clear_result("token")
    assert updater.read_result("token") is None


def test_preflight_rejects_dirty_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    install = tmp_path / "autoclip"
    install.mkdir()
    (install / ".git").mkdir()

    monkeypatch.setattr(updater.paths, "install_dir", lambda: install)
    monkeypatch.setattr(updater.shutil, "which", lambda command: command)
    monkeypatch.setattr(
        updater,
        "_capture",
        lambda command, cwd: " M frontend/src/App.tsx"
        if command[:2] == ["git", "status"]
        else "",
    )

    with pytest.raises(updater.UpdateError, match="local file changes"):
        updater.preflight()


def test_parent_wait_returns_for_missing_process() -> None:
    assert updater._wait_for_parent_exit(-1, timeout_s=0.01) is True
