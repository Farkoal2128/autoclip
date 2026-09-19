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

    with pytest.raises(updater.UpdateError, match="frontend/src/App.tsx"):
        updater.preflight()


def test_preflight_repairs_package_lock_only_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    install = tmp_path / "autoclip"
    install.mkdir()
    (install / ".git").mkdir()
    statuses = iter([" M frontend/package-lock.json", ""])
    commands: list[list[str]] = []

    monkeypatch.setattr(updater.paths, "install_dir", lambda: install)
    monkeypatch.setattr(updater.shutil, "which", lambda command: command)
    monkeypatch.setattr(
        updater,
        "_capture",
        lambda command, cwd: next(statuses)
        if command[:2] == ["git", "status"]
        else "",
    )
    monkeypatch.setattr(
        updater,
        "_run",
        lambda command, cwd: commands.append(command),
    )

    updater.preflight()

    assert commands == [
        ["git", "restore", "--worktree", "--", "frontend/package-lock.json"]
    ]


def test_preflight_does_not_discard_lockfile_alongside_source_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    install = tmp_path / "autoclip"
    install.mkdir()
    (install / ".git").mkdir()
    commands: list[list[str]] = []

    monkeypatch.setattr(updater.paths, "install_dir", lambda: install)
    monkeypatch.setattr(updater.shutil, "which", lambda command: command)
    monkeypatch.setattr(
        updater,
        "_capture",
        lambda command, cwd: (
            " M frontend/package-lock.json\n M frontend/src/App.tsx"
            if command[:2] == ["git", "status"]
            else ""
        ),
    )
    monkeypatch.setattr(
        updater,
        "_run",
        lambda command, cwd: commands.append(command),
    )

    with pytest.raises(updater.UpdateError, match="frontend/package-lock.json"):
        updater.preflight()

    assert commands == []


def test_parent_wait_returns_for_missing_process() -> None:
    assert updater._wait_for_parent_exit(-1, timeout_s=0.01) is True
