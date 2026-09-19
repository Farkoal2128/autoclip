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


def test_preflight_allows_dirty_checkout(
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
        "_status_entries",
        lambda _install: (_ for _ in ()).throw(
            AssertionError("preflight should not inspect checkout changes")
        ),
    )

    updater.preflight()


def test_stash_local_changes_cleans_generated_and_backs_up_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    install = tmp_path / "autoclip"
    install.mkdir()
    statuses = iter(
        [
            [
                (" M", "frontend/package-lock.json"),
                (" M", "frontend/src/App.tsx"),
            ],
            [(" M", "frontend/src/App.tsx")],
            [],
        ]
    )
    commands: list[list[str]] = []

    monkeypatch.setattr(updater, "_status_entries", lambda _install: next(statuses))
    monkeypatch.setattr(
        updater,
        "_run",
        lambda command, cwd: commands.append(command),
    )
    monkeypatch.setattr(
        updater,
        "_capture",
        lambda command, cwd: "abc123" if command[:2] == ["git", "rev-parse"] else "",
    )

    stash = updater._stash_local_changes(install, "token")

    assert stash == "abc123"
    assert commands == [
        [
            "git",
            "restore",
            "--source=HEAD",
            "--staged",
            "--worktree",
            "--",
            "frontend/package-lock.json",
        ],
        [
            "git",
            "stash",
            "push",
            "--include-untracked",
            "--message",
            "AutoClip automatic update backup token",
        ],
    ]


def test_stash_local_changes_discards_generated_files_without_stashing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    install = tmp_path / "autoclip"
    install.mkdir()
    generated = install / "frontend" / "tsconfig.tsbuildinfo"
    generated.parent.mkdir()
    generated.write_text("generated", encoding="utf-8")
    statuses = iter(
        [
            [("??", "frontend/tsconfig.tsbuildinfo")],
            [],
        ]
    )
    commands: list[list[str]] = []

    monkeypatch.setattr(updater, "_status_entries", lambda _install: next(statuses))
    monkeypatch.setattr(
        updater,
        "_run",
        lambda command, cwd: commands.append(command),
    )

    stash = updater._stash_local_changes(install, "token")

    assert stash is None
    assert not generated.exists()
    assert commands == []


def test_stash_local_changes_includes_untracked_source_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    install = tmp_path / "autoclip"
    install.mkdir()
    statuses = iter(
        [
            [("??", "frontend/src/local-experiment.ts")],
            [("??", "frontend/src/local-experiment.ts")],
            [],
        ]
    )
    commands: list[list[str]] = []

    monkeypatch.setattr(updater, "_status_entries", lambda _install: next(statuses))
    monkeypatch.setattr(
        updater,
        "_run",
        lambda command, cwd: commands.append(command),
    )
    monkeypatch.setattr(
        updater,
        "_capture",
        lambda command, cwd: "stash456",
    )

    stash = updater._stash_local_changes(install, "token")

    assert stash == "stash456"
    assert commands == [
        [
            "git",
            "stash",
            "push",
            "--include-untracked",
            "--message",
            "AutoClip automatic update backup token",
        ]
    ]



def test_parent_wait_returns_for_missing_process() -> None:
    assert updater._wait_for_parent_exit(-1, timeout_s=0.01) is True
