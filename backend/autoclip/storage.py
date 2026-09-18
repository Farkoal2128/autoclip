"""Management of AutoClip's relocatable media/work/export storage."""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .db import store

log = logging.getLogger(__name__)

DATA_DIRECTORIES = ("media", "work", "exports")
PREVIEW_MEDIA_SUFFIXES = {
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".m4v",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
}


class StorageError(RuntimeError):
    """A requested storage move could not be completed safely."""


@dataclass(frozen=True)
class StorageStatus:
    path: Path
    control_path: Path
    custom: bool
    managed_by_env: bool
    free_bytes: int
    total_bytes: int


@dataclass(frozen=True)
class _MovePlan:
    source: Path
    destination: Path
    copy_required: bool


def status() -> StorageStatus:
    target = paths.data_root()
    probe = _nearest_existing(target)
    usage = shutil.disk_usage(probe)
    return StorageStatus(
        path=target,
        control_path=paths.root(),
        custom=target != paths.root(),
        managed_by_env=bool(os.environ.get(paths.ENV_STORAGE_HOME)),
        free_bytes=usage.free,
        total_bytes=usage.total,
    )


def resolve_source_path(source) -> Path:
    """Return source media, repairing a stale absolute path after a storage move.

    Older or interrupted moves could leave the database pointing at the previous
    drive even though media/<source_id>/ was transferred. Prefer the stored path
    when it exists; otherwise recover the file from the current storage root and
    persist the repaired path for later preview, export, and retry operations.
    """
    stored = Path(source.path)
    if stored.is_file():
        return stored

    source_dir = paths.source_media_dir(source.id)
    direct = source_dir / stored.name
    if direct.is_file():
        recovered = direct
    elif source_dir.is_dir():
        files = [
            path
            for path in source_dir.iterdir()
            if path.is_file() and path.suffix.lower() in PREVIEW_MEDIA_SUFFIXES
        ]
        recovered = max(files, key=lambda path: path.stat().st_size) if files else stored
    else:
        recovered = stored

    if recovered != stored and recovered.is_file():
        log.warning(
            "Repairing stale source path for %s: %s -> %s",
            source.id,
            stored,
            recovered,
        )
        store.update_source_path(source.id, str(recovered))
        source.path = str(recovered)

    return recovered


def relocate(
    target: str | Path,
    *,
    on_status: Callable[[str], None] | None = None,
    on_progress: Callable[[float, str | None], None] | None = None,
) -> StorageStatus:
    """Move media/work/exports to target and update stored absolute paths.

    Relocation is deliberately two-phase. Files are copied and verified first;
    only after every directory is present at the destination do we update the
    database and storage pointer. The old copies are removed last.

    on_status emits human-readable milestones. on_progress emits an overall
    0..1 fraction plus the directory currently being processed.
    """
    def status_message(message: str) -> None:
        log.info("Storage move: %s", message)
        if on_status:
            on_status(message)

    def progress(fraction: float, folder: str | None = None) -> None:
        if on_progress:
            on_progress(max(0.0, min(1.0, fraction)), folder)

    if os.environ.get(paths.ENV_STORAGE_HOME):
        raise StorageError(
            f"{paths.ENV_STORAGE_HOME} is set; remove that environment override first."
        )

    status_message("Planning storage move")
    progress(0.0)

    old = paths.data_root().resolve()
    destination_root = Path(target).expanduser().resolve()
    if destination_root == old:
        status_message("Storage is already using this folder")
        progress(1.0)
        paths.ensure_layout()
        return status()

    if _contains(old, destination_root) or _contains(destination_root, old):
        raise StorageError("Choose a folder outside the current AutoClip storage tree.")

    destination_root.mkdir(parents=True, exist_ok=True)
    plans = _plan_move(old, destination_root)
    required_bytes = sum(_tree_size(plan.source) for plan in plans if plan.copy_required)
    free_bytes = shutil.disk_usage(_nearest_existing(destination_root)).free
    if required_bytes > free_bytes:
        raise StorageError(
            "The selected drive does not have enough free space for the move "
            f"({required_bytes:,} bytes needed, {free_bytes:,} bytes free)."
        )

    created_destinations: list[Path] = []
    database_rewritten = False
    pointer_changed = False
    copied_bytes = 0

    try:
        for plan in plans:
            folder = plan.source.name
            if not plan.copy_required:
                status_message(f"{folder}: already transferred; reusing destination copy")
                continue

            status_message(f"{folder}: copying files")
            if plan.destination.exists():
                plan.destination.rmdir()
            created_destinations.append(plan.destination)

            def copy_file(source, destination, *, folder_name=folder):
                nonlocal copied_bytes
                shutil.copyfile(source, destination)
                copied_bytes += Path(source).stat().st_size
                copy_fraction = copied_bytes / required_bytes if required_bytes else 1.0
                progress(copy_fraction * 0.85, folder_name)
                return destination

            shutil.copytree(plan.source, plan.destination, copy_function=copy_file)
            status_message(f"{folder}: verifying copied files")
            _verify_copy(plan.source, plan.destination)
            status_message(f"{folder}: verified")

        progress(0.88)
        status_message("Updating saved media, transcript, and export paths")
        store.rewrite_storage_paths(old, destination_root)
        database_rewritten = True
        progress(0.92)

        status_message("Switching AutoClip to the new storage folder")
        paths.set_data_root(destination_root)
        pointer_changed = True
        paths.ensure_layout()
        progress(0.96)

    except Exception as exc:
        if pointer_changed:
            try:
                paths.set_data_root(old)
            except Exception:
                log.exception("Could not restore the previous storage pointer.")
        if database_rewritten:
            try:
                store.rewrite_storage_paths(destination_root, old)
            except Exception:
                log.exception("Could not restore database paths after storage failure.")

        for destination in reversed(created_destinations):
            try:
                shutil.rmtree(destination, ignore_errors=False)
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("Could not clean partial storage copy %s.", destination)

        raise StorageError(f"Could not move AutoClip storage: {exc}") from exc

    cleanup_total = max(1, len(plans))
    for index, plan in enumerate(plans):
        folder = plan.source.name
        if plan.source.exists():
            status_message(f"{folder}: removing old copy")
            try:
                shutil.rmtree(plan.source, ignore_errors=False)
            except OSError as exc:
                log.warning(
                    "Storage moved successfully but old directory %s could not be removed: %s",
                    plan.source,
                    exc,
                )
                status_message(f"{folder}: old copy could not be removed; new copy is active")
        progress(0.96 + 0.03 * ((index + 1) / cleanup_total), folder)

    if old != paths.root():
        try:
            old.rmdir()
        except OSError:
            pass

    status_message("Storage move complete")
    progress(1.0)
    return status()


def _plan_move(old: Path, target: Path) -> list[_MovePlan]:
    plans: list[_MovePlan] = []

    for name in DATA_DIRECTORIES:
        source = old / name
        destination = target / name

        if source.exists() and not source.is_dir():
            raise StorageError(f"{source} exists and is not a directory.")
        if destination.exists() and not destination.is_dir():
            raise StorageError(f"{destination} exists and is not a directory.")

        source_has_files = _has_files(source)
        destination_has_files = _has_files(destination)

        if source_has_files and destination_has_files:
            if _size_manifest(source) == _size_manifest(destination):
                plans.append(_MovePlan(source, destination, copy_required=False))
                continue
            raise StorageError(
                f"{destination} already contains different files while {source} still has data. "
                "Choose another folder, or empty the destination subfolder and try again."
            )

        if destination_has_files and not source_has_files:
            plans.append(_MovePlan(source, destination, copy_required=False))
        elif source.exists():
            plans.append(_MovePlan(source, destination, copy_required=True))
        else:
            plans.append(_MovePlan(source, destination, copy_required=False))

    return plans


def _verify_copy(source: Path, destination: Path) -> None:
    if _size_manifest(source) != _size_manifest(destination):
        raise StorageError(
            f"Verification failed while copying {source.name}; the destination is incomplete."
        )


def _size_manifest(root: Path) -> dict[str, int]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.stat().st_size
        for path in root.rglob("*")
        if path.is_file()
    }


def _tree_size(root: Path) -> int:
    return sum(_size_manifest(root).values())


def _has_files(root: Path) -> bool:
    if not root.is_dir():
        return False
    return any(path.is_file() for path in root.rglob("*"))


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return parent != child
