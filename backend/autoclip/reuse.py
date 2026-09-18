"""Helpers for highlight reruns that reuse immutable analysis artifacts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths

META_KEY = "_autoclip_highlight_rerun"


class ArtifactReuseError(RuntimeError):
    """A rerun could not safely reuse the previous job's analysis files."""


@dataclass(frozen=True)
class RerunMeta:
    source_job_id: str | None
    exclude_job_ids: tuple[str, ...]
    exclude_ranges: tuple[tuple[int, int], ...]
    pass_number: int


def rerun_meta(job) -> RerunMeta:
    raw = job.settings.get(META_KEY, {}) if isinstance(job.settings, dict) else {}
    if not isinstance(raw, dict):
        raw = {}

    source_job_id = str(raw.get("source_job_id") or "").strip() or None
    exclude_raw = raw.get("exclude_job_ids") or []
    exclude_job_ids = tuple(
        str(item)
        for item in exclude_raw
        if isinstance(item, str) and item.strip()
    )

    ranges: list[tuple[int, int]] = []
    for item in raw.get("exclude_ranges") or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            start, end = int(item[0]), int(item[1])
        except (TypeError, ValueError):
            continue
        if start >= 0 and end >= start:
            ranges.append((start, end))

    try:
        pass_number = max(1, int(raw.get("pass_number") or 1))
    except (TypeError, ValueError):
        pass_number = 1

    return RerunMeta(
        source_job_id=source_job_id,
        exclude_job_ids=exclude_job_ids,
        exclude_ranges=tuple(ranges),
        pass_number=pass_number,
    )


def build_rerun_settings(
    parent_job,
    settings_payload: dict[str, Any],
    parent_clip_ranges: list[tuple[int, int]],
) -> dict[str, Any]:
    """Attach rerun metadata while preserving the normal Settings snapshot."""
    previous = rerun_meta(parent_job)

    inherited_ids = list(previous.exclude_job_ids)
    inherited_ids.append(parent_job.id)
    exclude_job_ids = list(dict.fromkeys(inherited_ids))

    inherited_ranges = [*previous.exclude_ranges, *parent_clip_ranges]
    exclude_ranges = list(dict.fromkeys(inherited_ranges))

    payload = dict(settings_payload)
    payload[META_KEY] = {
        "source_job_id": parent_job.id,
        "exclude_job_ids": exclude_job_ids,
        "exclude_ranges": [list(item) for item in exclude_ranges],
        "pass_number": previous.pass_number + 1,
    }
    return payload


def seed_analysis_artifacts(source_job_id: str, target_job_id: str) -> None:
    """Hard-link cached audio/transcript into a new job workspace.

    Hard links give each project an independent path while keeping one physical
    copy of the large audio file. Removing either project only removes its link;
    the other project keeps working.
    """
    source_root = paths.job_work_dir(source_job_id)
    target_root = paths.job_work_dir(target_job_id)

    required = ("audio.wav", "transcript.json")
    missing = [name for name in required if not (source_root / name).is_file()]
    if missing:
        joined = ", ".join(missing)
        raise ArtifactReuseError(
            f"The previous project is missing reusable analysis files: {joined}."
        )

    target_root.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        for name in (*required, "silences.json"):
            source = source_root / name
            if not source.is_file():
                continue
            destination = target_root / name
            os.link(source, destination)
            created.append(destination)
    except OSError as exc:
        for destination in reversed(created):
            destination.unlink(missing_ok=True)
        try:
            target_root.rmdir()
        except OSError:
            pass
        raise ArtifactReuseError(
            "AutoClip could not create space-saving links to the existing audio and "
            "transcript. The storage filesystem must support hard links; no duplicate "
            "audio file was created."
        ) from exc
