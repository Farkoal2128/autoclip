"""Pydantic request and response models for the REST API.

Kept separate from the dataclasses in ``db.models``: those mirror SQLite rows,
these are the wire contract. Splitting them means a schema change doesn't
silently reshape the API, and the API can expose derived fields (durations,
export URLs) that don't belong in storage.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import reuse
from ..db import models

_TRANSCRIPT_INDEX_TAG = re.compile(r"\[\d+\]\s*")


def _clean_clip_text(value: str) -> str:
    """Hide transcript index markers accidentally persisted by older models."""
    return _TRANSCRIPT_INDEX_TAG.sub("", value or "").strip()


class SourceOut(BaseModel):
    id: str
    type: str
    title: str
    url: str | None = None
    filename: str | None = None
    channel: str | None = None
    duration_s: float
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_audio: bool
    has_video: bool
    created_at: str

    @classmethod
    def of(cls, source: models.Source) -> SourceOut:
        # Built field by field rather than from vars(): the row carries an
        # absolute filesystem path that has no business leaving the process.
        return cls(
            id=source.id,
            type=source.type,
            title=source.title,
            url=source.url,
            filename=source.filename,
            channel=source.channel,
            duration_s=source.duration_s,
            width=source.width,
            height=source.height,
            fps=source.fps,
            has_audio=source.has_audio,
            has_video=source.has_video,
            created_at=source.created_at,
        )


class RemoteIngestIn(BaseModel):
    url: str
    cookies_from_browser: str | None = None


class YouTubeIngestIn(RemoteIngestIn):
    """Legacy request shape kept for the /youtube endpoint."""


class JobSettingsIn(BaseModel):
    """Per-job overrides. Anything omitted falls back to saved settings."""

    provider: str | None = None
    whisper_model: str | None = None
    language: str | None = None
    diarization: bool | None = None
    min_duration_s: float | None = Field(default=None, gt=0)
    max_duration_s: float | None = Field(default=None, gt=0)
    max_clips: int | None = Field(default=None, ge=1, le=50)
    caption_style: str | None = None
    ratio: Literal["9:16", "1:1", "16:9"] | None = None


class JobCreateIn(BaseModel):
    source_id: str
    settings: JobSettingsIn = Field(default_factory=JobSettingsIn)


class JobOut(BaseModel):
    id: str
    source_id: str
    status: str
    current_stage: str
    progress: float
    error: str | None = None
    provider: str
    highlight_pass: int = 1
    reused_from_job_id: str | None = None
    reused_analysis: bool = False
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    source: SourceOut | None = None

    @classmethod
    def of(cls, job: models.Job, source: models.Source | None = None) -> JobOut:
        rerun = reuse.rerun_meta(job)
        return cls(
            id=job.id,
            source_id=job.source_id,
            status=job.status,
            current_stage=job.current_stage,
            progress=job.progress,
            error=job.error,
            provider=job.provider,
            highlight_pass=rerun.pass_number,
            reused_from_job_id=rerun.source_job_id,
            reused_analysis=rerun.source_job_id is not None,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            source=SourceOut.of(source) if source else None,
        )


class WordOut(BaseModel):
    text: str
    start: float
    end: float
    speaker: str | None = None


class CutRange(BaseModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)


class NormalizedRect(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)


class LayoutRegion(BaseModel):
    id: str
    label: str = ""
    source: NormalizedRect
    destination: NormalizedRect


class LayoutFrame(BaseModel):
    base_center_x: float = Field(default=0.5, ge=0, le=1)
    base_center_y: float = Field(default=0.5, ge=0, le=1)
    overlays: list[LayoutRegion] = Field(default_factory=list, max_length=6)


class LayoutCue(BaseModel):
    """A complete layout snapshot that becomes active at a source timestamp."""

    id: str
    at_s: float = Field(ge=0)
    transition: Literal["cut", "glide"] = "cut"
    lead_s: float = Field(default=1, ge=0, le=30)
    layout: LayoutFrame


class ManualLayout(LayoutFrame):
    """Starting layout plus optional timestamped layout snapshots."""

    cues: list[LayoutCue] = Field(default_factory=list, max_length=32)


class ExportOut(BaseModel):
    id: str
    clip_id: str
    ratio: str
    style: str
    size_bytes: int
    created_at: str
    download_url: str

    @classmethod
    def of(cls, export: models.Export) -> ExportOut:
        return cls(
            id=export.id,
            clip_id=export.clip_id,
            ratio=export.ratio,
            style=export.style,
            size_bytes=export.size_bytes,
            created_at=export.created_at,
            download_url=f"/api/exports/{export.id}/download",
        )


class ClipOut(BaseModel):
    id: str
    job_id: str
    rank: int
    start_s: float
    end_s: float
    duration_s: float
    start_word: int
    end_word: int
    title: str
    hook: str
    score: int
    reason: str
    status: str
    user_trimmed: bool
    caption_style: str = "bold_pop"
    ratio: str = "9:16"
    burn_captions: bool = True
    cuts: list[CutRange] = Field(default_factory=list)
    layout: ManualLayout | None = None
    exports: list[ExportOut] = Field(default_factory=list)

    @classmethod
    def of(
        cls,
        clip: models.Clip,
        *,
        edit: models.ClipEdit | None = None,
        exports: list[models.Export] | None = None,
    ) -> ClipOut:
        return cls(
            id=clip.id,
            job_id=clip.job_id,
            rank=clip.rank,
            start_s=clip.start_s,
            end_s=clip.end_s,
            duration_s=max(
                0.0, clip.duration_s - (edit.cut_duration_s if edit else 0.0)
            ),
            start_word=clip.start_word,
            end_word=clip.end_word,
            title=_clean_clip_text(clip.title),
            hook=_clean_clip_text(clip.hook),
            score=clip.score,
            reason=_clean_clip_text(clip.reason),
            status=clip.status,
            user_trimmed=clip.user_trimmed,
            caption_style=edit.caption_style if edit else "bold_pop",
            ratio=edit.ratio if edit else "9:16",
            burn_captions=edit.burn_captions if edit else True,
            cuts=[CutRange(**cut) for cut in (edit.cuts if edit else [])],
            layout=ManualLayout(**edit.layout) if edit and edit.layout else None,
            exports=[ExportOut.of(e) for e in (exports or [])],
        )


class ClipPatchIn(BaseModel):
    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, gt=0)
    title: str | None = None
    status: Literal["candidate", "kept", "discarded", "exported"] | None = None


class CaptionPatchIn(BaseModel):
    """Word-level caption edits for one clip."""

    words: list[WordOut] | None = None
    caption_style: str | None = None
    ratio: Literal["9:16", "1:1", "16:9"] | None = None
    burn_captions: bool | None = None


class CutPatchIn(BaseModel):
    """Source-time spans to remove from the middle of a clip."""

    cuts: list[CutRange] = Field(default_factory=list)


class LayoutPatchIn(BaseModel):
    """Optional manual composition used for 9:16 and 1:1 exports."""

    layout: ManualLayout | None = None
    ratio: Literal["9:16", "1:1"] | None = None


class LayoutPresetCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    ratio: Literal["9:16", "1:1"]
    layout: ManualLayout


class LayoutPresetOut(BaseModel):
    id: str
    name: str
    ratio: Literal["9:16", "1:1"]
    layout: ManualLayout


class ExportRequestIn(BaseModel):
    ratio: Literal["9:16", "1:1", "16:9"] = "9:16"
    style: str = "bold_pop"
    write_srt: bool = False



class CaptionStyleOut(BaseModel):
    key: str
    label: str
    description: str
    #: Enough for the UI to approximate the look in HTML/CSS before rendering.
    preview: dict[str, Any]


class ProviderStatusOut(BaseModel):
    name: str
    available: bool
    detail: str = ""
    models: list[str] = Field(default_factory=list)
    requires_key: bool = True
    has_key: bool = False


class SettingsOut(BaseModel):
    active_provider: str
    providers: dict[str, dict[str, Any]]
    whisper: dict[str, Any]
    clips: dict[str, Any]
    ingest: dict[str, Any]
    export: dict[str, Any]
    insecure_secret_storage: bool
    #: Which providers have a key stored. The keys themselves never leave the
    #: keyring, so the UI shows presence, not value.
    keys_present: dict[str, bool] = Field(default_factory=dict)


class SettingsIn(BaseModel):
    active_provider: str | None = None
    providers: dict[str, dict[str, Any]] | None = None
    whisper: dict[str, Any] | None = None
    clips: dict[str, Any] | None = None
    ingest: dict[str, Any] | None = None
    export: dict[str, Any] | None = None


class SecretIn(BaseModel):
    key: str
    value: str


class StorageOut(BaseModel):
    path: str
    control_path: str
    custom: bool
    managed_by_env: bool
    free_bytes: int
    total_bytes: int


class StorageMoveIn(BaseModel):
    path: str


class FolderChoiceOut(BaseModel):
    path: str | None = None


class DesktopShortcutOut(BaseModel):
    supported: bool
    exists: bool
    path: str | None = None


class SystemOut(BaseModel):
    ready: bool
    python_version: str
    platform: str
    ffmpeg_version: str | None
    has_libass: bool
    nvenc_works: bool
    accel: str
    gpu_name: str | None
    compute_type: str
    diarization_available: bool
