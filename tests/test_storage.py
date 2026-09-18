"""Relocatable media/work/export storage."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from autoclip import paths, storage


def _seed_storage() -> None:
    paths.ensure_layout()
    (paths.media_dir() / "media.txt").write_text("media", encoding="utf-8")
    (paths.work_dir() / "work.txt").write_text("work", encoding="utf-8")
    (paths.exports_dir() / "export.txt").write_text("export", encoding="utf-8")


def test_relocate_copies_all_directories_before_switching(
    initialised_db: int, tmp_path: Path
) -> None:
    _seed_storage()
    old = paths.data_root()
    target = tmp_path / "target"

    result = storage.relocate(target)

    assert result.path == target.resolve()
    assert paths.data_root() == target.resolve()
    assert (target / "media" / "media.txt").read_text(encoding="utf-8") == "media"
    assert (target / "work" / "work.txt").read_text(encoding="utf-8") == "work"
    assert (target / "exports" / "export.txt").read_text(encoding="utf-8") == "export"
    assert not (old / "media").exists()
    assert not (old / "work").exists()
    assert not (old / "exports").exists()


def test_relocate_resumes_after_media_was_already_moved(
    initialised_db: int, tmp_path: Path
) -> None:
    _seed_storage()
    old = paths.data_root()
    target = tmp_path / "target"
    target.mkdir()

    # Reproduce the failure mode from the original implementation: media moved
    # successfully, then the request failed before work/exports were transferred.
    shutil.move(str(old / "media"), str(target / "media"))

    result = storage.relocate(target)

    assert result.path == target.resolve()
    assert (target / "media" / "media.txt").read_text(encoding="utf-8") == "media"
    assert (target / "work" / "work.txt").read_text(encoding="utf-8") == "work"
    assert (target / "exports" / "export.txt").read_text(encoding="utf-8") == "export"


def test_relocate_accepts_identical_preexisting_copy(
    initialised_db: int, tmp_path: Path
) -> None:
    _seed_storage()
    old = paths.data_root()
    target = tmp_path / "target"
    shutil.copytree(old / "media", target / "media")

    result = storage.relocate(target)

    assert result.path == target.resolve()
    assert (target / "media" / "media.txt").read_text(encoding="utf-8") == "media"
    assert not (old / "media").exists()


def test_relocate_refuses_two_different_nonempty_versions_of_same_directory(
    initialised_db: int, tmp_path: Path
) -> None:
    _seed_storage()
    target = tmp_path / "target"
    (target / "media").mkdir(parents=True)
    (target / "media" / "other.txt").write_text("different", encoding="utf-8")

    with pytest.raises(storage.StorageError, match="different files"):
        storage.relocate(target)

    assert (paths.media_dir() / "media.txt").exists()


def test_relocate_reports_progress_and_completion(
    initialised_db: int, tmp_path: Path
) -> None:
    _seed_storage()
    target = tmp_path / "target"
    messages: list[str] = []
    progress: list[tuple[float, str | None]] = []

    storage.relocate(
        target,
        on_status=messages.append,
        on_progress=lambda fraction, folder: progress.append((fraction, folder)),
    )

    assert messages[0] == "Planning storage move"
    assert "media: copying files" in messages
    assert "work: verified" in messages
    assert "exports: verified" in messages
    assert messages[-1] == "Storage move complete"
    assert progress[0][0] == pytest.approx(0.0)
    assert progress[-1][0] == pytest.approx(1.0)
    assert all(a[0] <= b[0] for a, b in zip(progress, progress[1:]))
