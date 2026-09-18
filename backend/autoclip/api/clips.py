"""Clip review, editing, preview media, and export."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import paths
from ..config import load as load_settings
from ..db import store
from ..db.models import Export, new_id
from ..pipeline import captions as captions_module
from ..pipeline import export as export_module
from ..pipeline.reframe.croppath import CropPath, centre_crop
from ..pipeline.runner import JobWorkspace
from ..pipeline.transcript import Transcript, Word
from .schemas import (
    CaptionPatchIn,
    CaptionStyleOut,
    ClipOut,
    ClipPatchIn,
    CutPatchIn,
    ExportOut,
    LayoutPatchIn,
    ExportRequestIn,
    WordOut,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["clips"])


def _clip_out(clip) -> ClipOut:
    edit = store.get_clip_edit(clip.id)
    exports = store.list_exports(clip.id)
    return ClipOut.of(clip, edit=edit, exports=exports)


def _normalise_cuts(
    cuts, start_s: float, end_s: float, *, strict: bool = True
) -> list[dict]:
    """Sort and merge source-time cut ranges, keeping middle cuts inside the clip."""
    normalised: list[dict[str, float]] = []
    for cut in cuts:
        raw_start = float(cut.start_s if hasattr(cut, "start_s") else cut["start_s"])
        raw_end = float(cut.end_s if hasattr(cut, "end_s") else cut["end_s"])

        if raw_end <= raw_start:
            raise HTTPException(status_code=400, detail="Cut end must come after cut start.")
        if strict and (raw_start <= start_s + 0.01 or raw_end >= end_s - 0.01):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Middle cuts must stay inside the clip. "
                    "Use the trim handles for the start or end."
                ),
            )

        cut_start = max(start_s, raw_start)
        cut_end = min(end_s, raw_end)
        if cut_end - cut_start <= 0.01:
            continue

        normalised.append({"start_s": cut_start, "end_s": cut_end})

    normalised.sort(key=lambda item: item["start_s"])
    merged: list[dict[str, float]] = []
    for cut in normalised:
        if merged and cut["start_s"] <= merged[-1]["end_s"] + 0.01:
            merged[-1]["end_s"] = max(merged[-1]["end_s"], cut["end_s"])
        else:
            merged.append(cut)

    removed = sum(cut["end_s"] - cut["start_s"] for cut in merged)
    if strict and (end_s - start_s - removed) < 0.5:
        raise HTTPException(
            status_code=400, detail="Cuts must leave at least 0.5 seconds of video."
        )
    return merged


async def list_clips_for_job(job_id: str) -> list[ClipOut]:
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    clips = await asyncio.to_thread(store.list_clips, job_id)
    return [await asyncio.to_thread(_clip_out, clip) for clip in clips]


@router.get("/clips/{clip_id}", response_model=ClipOut)
async def get_clip(clip_id: str) -> ClipOut:
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")
    return await asyncio.to_thread(_clip_out, clip)


@router.get("/clips/{clip_id}/words", response_model=list[WordOut])
async def clip_words(clip_id: str) -> list[WordOut]:
    """Words for a clip — the caption editor's source of truth.

    Returns the user's edited words when they exist, falling back to the
    transcript's originals.
    """
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    edit = await asyncio.to_thread(store.get_clip_edit, clip_id)
    if edit is not None and edit.edited_words is not None:
        return [WordOut(**word) for word in edit.edited_words]

    transcript = await asyncio.to_thread(_load_transcript, clip.job_id)
    words = transcript.slice(clip.start_word, clip.end_word)
    return [WordOut(text=w.text, start=w.start, end=w.end, speaker=w.speaker) for w in words]


@router.get("/clips/{clip_id}/crop-path")
async def clip_crop_path(clip_id: str) -> dict:
    """The computed crop path for a clip, so the preview can match the export.

    Without this the review player can only centre-crop, which on a tracked shot
    shows different framing from the file that gets rendered — sometimes with
    the speaker half out of frame. Reviewing framing against the wrong framing
    is worse than not previewing it at all.
    """
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    cached = JobWorkspace(clip.job_id).crop_path(clip.id)
    if not cached.exists():
        # Audio-only sources and pre-reframe jobs legitimately have none; the
        # client falls back to a centre crop.
        raise HTTPException(status_code=404, detail="No crop path for this clip yet.")

    return (await asyncio.to_thread(CropPath.load, cached)).to_dict()


@router.patch("/clips/{clip_id}", response_model=ClipOut)
async def patch_clip(clip_id: str, payload: ClipPatchIn) -> ClipOut:
    """Trim, retitle, or keep/discard a clip.

    A trim re-derives the word range from the new times, so captions stay in
    step with the boundaries without the client having to compute indices.
    """
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    start_s = payload.start_s if payload.start_s is not None else clip.start_s
    end_s = payload.end_s if payload.end_s is not None else clip.end_s
    if end_s <= start_s:
        raise HTTPException(status_code=400, detail="End must come after start.")

    trimmed = payload.start_s is not None or payload.end_s is not None
    start_word = end_word = None

    if trimmed:
        transcript = await asyncio.to_thread(_load_transcript, clip.job_id)
        first, last = transcript.indices_in_range(start_s, end_s)
        start_word, end_word = first, max(first, last - 1)

    await asyncio.to_thread(
        store.update_clip,
        clip_id,
        start_s=start_s if trimmed else None,
        end_s=end_s if trimmed else None,
        start_word=start_word,
        end_word=end_word,
        title=payload.title,
        status=payload.status,
        user_trimmed=True if trimmed else None,
    )

    if trimmed:
        existing = await asyncio.to_thread(store.get_clip_edit, clip_id)
        if existing is not None and existing.cuts:
            existing.cuts = _normalise_cuts(existing.cuts, start_s, end_s, strict=False)
            await asyncio.to_thread(store.upsert_clip_edit, existing)

    updated = await asyncio.to_thread(store.get_clip, clip_id)
    return await asyncio.to_thread(_clip_out, updated)


@router.patch("/clips/{clip_id}/captions", response_model=ClipOut)
async def patch_captions(clip_id: str, payload: CaptionPatchIn) -> ClipOut:
    """Store word-level caption edits and style choices for a clip."""
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    if payload.caption_style is not None:
        try:
            captions_module.get_style(payload.caption_style)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing = await asyncio.to_thread(store.get_clip_edit, clip_id)
    from ..db.models import ClipEdit

    edit = ClipEdit(
        clip_id=clip_id,
        edited_words=(
            [w.model_dump() for w in payload.words]
            if payload.words is not None
            else (existing.edited_words if existing else None)
        ),
        cuts=existing.cuts if existing else [],
        caption_style=payload.caption_style or (existing.caption_style if existing else "bold_pop"),
        ratio=payload.ratio or (existing.ratio if existing else "9:16"),
        burn_captions=(
            payload.burn_captions
            if payload.burn_captions is not None
            else (existing.burn_captions if existing else True)
        ),
        layout=existing.layout if existing else None,
    )
    await asyncio.to_thread(store.upsert_clip_edit, edit)

    return await asyncio.to_thread(_clip_out, clip)


@router.patch("/clips/{clip_id}/cuts", response_model=ClipOut)
async def patch_cuts(clip_id: str, payload: CutPatchIn) -> ClipOut:
    """Save non-destructive ranges that should be removed from the rendered clip."""
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    cuts = _normalise_cuts(payload.cuts, clip.start_s, clip.end_s)
    existing = await asyncio.to_thread(store.get_clip_edit, clip_id)
    from ..db.models import ClipEdit

    edit = ClipEdit(
        clip_id=clip_id,
        edited_words=existing.edited_words if existing else None,
        cuts=cuts,
        caption_style=existing.caption_style if existing else "bold_pop",
        ratio=existing.ratio if existing else "9:16",
        burn_captions=existing.burn_captions if existing else True,
        layout=existing.layout if existing else None,
    )
    await asyncio.to_thread(store.upsert_clip_edit, edit)
    return await asyncio.to_thread(_clip_out, clip)


def _validate_layout(layout) -> None:
    for region in layout.overlays:
        for name, rect in (("source", region.source), ("destination", region.destination)):
            if rect.x + rect.width > 1.000001 or rect.y + rect.height > 1.000001:
                label = region.label or region.id
                raise HTTPException(
                    status_code=400,
                    detail=f"Layout region {label!r} {name} rectangle exceeds its frame.",
                )


@router.patch("/clips/{clip_id}/layout", response_model=ClipOut)
async def patch_layout(clip_id: str, payload: LayoutPatchIn) -> ClipOut:
    """Save or clear a manual 9:16/1:1 crop-and-overlay composition."""
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    existing = await asyncio.to_thread(store.get_clip_edit, clip_id)
    from ..db.models import ClipEdit

    ratio = existing.ratio if existing else "9:16"
    if payload.layout is not None:
        if ratio not in ("9:16", "1:1"):
            raise HTTPException(
                status_code=400,
                detail="Custom crop layouts are available only for 9:16 and 1:1 clips.",
            )
        _validate_layout(payload.layout)

    edit = ClipEdit(
        clip_id=clip_id,
        edited_words=existing.edited_words if existing else None,
        cuts=existing.cuts if existing else [],
        caption_style=existing.caption_style if existing else "bold_pop",
        ratio=ratio,
        burn_captions=existing.burn_captions if existing else True,
        layout=payload.layout.model_dump() if payload.layout is not None else None,
    )
    await asyncio.to_thread(store.upsert_clip_edit, edit)
    return await asyncio.to_thread(_clip_out, clip)


@router.post("/clips/{clip_id}/export", response_model=ExportOut, status_code=201)
async def export_clip(clip_id: str, payload: ExportRequestIn) -> ExportOut:
    """Render one clip and return its download link."""
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    job = await asyncio.to_thread(store.get_job, clip.job_id)
    source = await asyncio.to_thread(store.get_source, job.source_id) if job else None
    if job is None or source is None:
        raise HTTPException(status_code=404, detail="The clip's source is missing.")

    try:
        style = captions_module.get_style(payload.style)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    words = [
        Word(text=w.text, start=w.start, end=w.end, speaker=w.speaker)
        for w in await clip_words(clip_id)
    ]

    settings = load_settings()
    settings.export.write_srt = payload.write_srt

    workspace = JobWorkspace(clip.job_id)
    crop_path = await asyncio.to_thread(_crop_path_for, workspace, clip, source, payload.ratio)

    destination = (
        paths.exports_dir()
        / clip.job_id
        / export_module.output_filename(clip.title or f"clip-{clip.rank}", payload.ratio)
    )

    edit = await asyncio.to_thread(store.get_clip_edit, clip_id)
    cuts = [
        (float(cut["start_s"]), float(cut["end_s"]))
        for cut in (edit.cuts if edit else [])
    ]

    request = export_module.ExportRequest(
        source=Path(source.path),
        destination=destination,
        start_s=clip.start_s,
        end_s=clip.end_s,
        crop_path=crop_path,
        words=words,
        style=style,
        ratio=payload.ratio,
        burn_captions=edit.burn_captions if edit else True,
        cuts=cuts,
        layout=(
            export_module.ManualLayout.from_dict(edit.layout)
            if edit and edit.layout and payload.ratio in ("9:16", "1:1")
            else None
        ),
    )

    try:
        await asyncio.to_thread(
            export_module.export_clip,
            request,
            work_dir=workspace.captions_dir,
            settings=settings.export,
        )
    except export_module.ExportError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    record = Export(
        id=new_id(),
        clip_id=clip_id,
        path=str(destination),
        ratio=payload.ratio,
        style=style.key,
        size_bytes=destination.stat().st_size,
    )
    await asyncio.to_thread(store.create_export, record)
    await asyncio.to_thread(store.update_clip, clip_id, status="exported")

    return ExportOut.of(record)


@router.get("/exports/{export_id}/download")
async def download_export(export_id: str) -> FileResponse:
    record = await asyncio.to_thread(store.get_export, export_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Export not found.")

    path = Path(record.path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="The exported file has been moved or deleted.")

    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.get("/jobs/{job_id}/media")
async def job_media(job_id: str) -> FileResponse:
    """Serve the source video, so the review player can scrub the original.

    Range requests are handled by FileResponse, which is what makes seeking in
    the preview player usable on a long source.
    """
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    source = await asyncio.to_thread(store.get_source, job.source_id)
    if source is None or not Path(source.path).exists():
        raise HTTPException(status_code=404, detail="Source media not found.")

    return FileResponse(Path(source.path))


@router.get("/caption-styles", response_model=list[CaptionStyleOut])
async def caption_styles() -> list[CaptionStyleOut]:
    """Style presets, with enough detail for the UI to approximate them in CSS."""
    return [
        CaptionStyleOut(
            key=style.key,
            label=style.label,
            description=style.description,
            preview={
                "font": style.font,
                "primary": style.primary,
                "accent": style.accent,
                "outline": style.outline,
                "outlineWidth": style.outline_width,
                "shadow": style.shadow,
                "bold": style.bold,
                "allCaps": style.all_caps,
                "sizeRatio": style.size_ratio,
                "marginRatio": style.margin_v_ratio,
                "boxed": style.boxed,
                "boxColour": style.box_colour,
                "boxAlpha": style.box_alpha,
                "animation": style.animation,
                "scalePercent": style.scale_percent,
                "maxWords": style.max_words,
            },
        )
        for style in captions_module.PRESETS.values()
    ]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _load_transcript(job_id: str) -> Transcript:
    workspace = JobWorkspace(job_id)
    if not workspace.transcript.exists():
        raise HTTPException(status_code=404, detail="This job has no transcript yet.")
    return Transcript.load(workspace.transcript)


def _crop_path_for(workspace: JobWorkspace, clip, source, ratio: str) -> CropPath:
    """Reuse the job's cached crop path, falling back to a centre crop.

    A re-export at a different ratio can't reuse a path computed for the
    original one, so the geometry is recomputed rather than stretched.
    """
    cached = workspace.crop_path(clip.id)
    if cached.exists() and ratio == "9:16":
        return CropPath.load(cached)

    aspect = {"9:16": (9, 16), "1:1": (1, 1), "16:9": (16, 9)}[ratio]
    return centre_crop(
        source.width or 1920,
        source.height or 1080,
        clip.end_s - clip.start_s,
        aspect_w=aspect[0],
        aspect_h=aspect[1],
    )
