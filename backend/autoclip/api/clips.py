"""Clip review, editing, preview media, and export."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import paths, storage, twitch_chat
from ..config import load as load_settings
from ..db import store
from ..db.models import Export, new_id
from ..pipeline import captions as captions_module
from ..pipeline import export as export_module
from ..pipeline import ingest as ingest_module
from ..pipeline.reframe.croppath import CropPath, centre_crop
from ..pipeline.runner import JobWorkspace
from ..pipeline.transcript import Transcript, Word
from .schemas import (
    CaptionPatchIn,
    CaptionStyleOut,
    ClipOut,
    ClipPatchIn,
    CutPatchIn,
    DeletedClipsOut,
    ExportArchiveOut,
    ExportOut,
    ExportRequestIn,
    LayoutPatchIn,
    TwitchChatMessageOut,
    WordOut,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["clips"])

_MEDIA_PREP_LOCKS: dict[str, asyncio.Lock] = {}
_MEDIA_COMPATIBILITY_CACHE: dict[str, tuple[int, int, bool]] = {}


def _preview_needs_proxy(path: Path) -> bool:
    stat = path.stat()
    key = str(path)
    cached = _MEDIA_COMPATIBILITY_CACHE.get(key)
    fingerprint = (stat.st_mtime_ns, stat.st_size)
    if cached is not None and cached[:2] == fingerprint:
        return cached[2]

    needs_proxy = ingest_module.browser_preview_needs_proxy(path)
    _MEDIA_COMPATIBILITY_CACHE[key] = (*fingerprint, needs_proxy)
    return needs_proxy


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


@router.get("/clips/{clip_id}/twitch-chat", response_model=list[TwitchChatMessageOut])
async def clip_twitch_chat(clip_id: str) -> list[TwitchChatMessageOut]:
    """Load chat replay for this clip, only when explicitly requested by the editor."""
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    job = await asyncio.to_thread(store.get_job, clip.job_id)
    source = await asyncio.to_thread(store.get_source, job.source_id) if job else None
    vod_id = twitch_chat.vod_id_from_url(source.url or "") if source else None
    if job is None or source is None or vod_id is None:
        raise HTTPException(
            status_code=404,
            detail="Twitch chat is available only for projects created from Twitch VOD URLs.",
        )

    cache_path = JobWorkspace(clip.job_id).twitch_chat(clip.id)
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                cached.get("version") == twitch_chat.CHAT_CACHE_VERSION
                and cached.get("vod_id") == vod_id
                and abs(float(cached.get("start_s", -1)) - clip.start_s) < 0.001
                and abs(float(cached.get("end_s", -1)) - clip.end_s) < 0.001
            ):
                return [
                    TwitchChatMessageOut(**item)
                    for item in cached.get("messages", [])
                ]
        except (OSError, TypeError, ValueError):
            log.warning("Ignoring unreadable Twitch chat cache for clip %s.", clip.id)

    try:
        messages = await twitch_chat.fetch_vod_chat(
            vod_id,
            start_s=clip.start_s,
            end_s=clip.end_s,
        )
        messages = await twitch_chat.cache_message_assets(
            messages,
            JobWorkspace(clip.job_id).twitch_chat_assets(clip.id),
        )
    except twitch_chat.TwitchChatError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    payload = {
        "version": twitch_chat.CHAT_CACHE_VERSION,
        "vod_id": vod_id,
        "start_s": clip.start_s,
        "end_s": clip.end_s,
        "messages": [message.to_dict() for message in messages],
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        cache_path.write_text,
        json.dumps(payload, ensure_ascii=False),
        "utf-8",
    )
    return [TwitchChatMessageOut(**message.to_dict()) for message in messages]


@router.get("/clips/{clip_id}/twitch-chat-assets/{asset_id}")
async def twitch_chat_asset(clip_id: str, asset_id: str) -> FileResponse:
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")
    if not twitch_chat.is_valid_asset_id(asset_id):
        raise HTTPException(status_code=404, detail="Twitch chat asset not found.")

    path = JobWorkspace(clip.job_id).twitch_chat_assets(clip.id) / asset_id
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Twitch chat asset not found.")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/clips/{clip_id}/crop-path")
async def clip_crop_path(clip_id: str) -> dict:
    """Return the static center crop used by Reframe and export.

    The cached file is still the stage-completion artifact, but its old tracked
    geometry is intentionally ignored so projects created before automatic subject
    tracking was removed now preview the same static framing as new exports.
    """
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    cached = JobWorkspace(clip.job_id).crop_path(clip.id)
    if not cached.exists():
        # Audio-only sources and pre-reframe jobs legitimately have none; the
        # client falls back to a centre crop.
        raise HTTPException(status_code=404, detail="No crop path for this clip yet.")

    job = await asyncio.to_thread(store.get_job, clip.job_id)
    source = await asyncio.to_thread(store.get_source, job.source_id) if job else None
    if job is None or source is None:
        raise HTTPException(status_code=404, detail="The clip's source is missing.")

    edit = await asyncio.to_thread(store.get_clip_edit, clip_id)
    ratio = edit.ratio if edit else "9:16"
    return (await asyncio.to_thread(_crop_path_for, clip, source, ratio)).to_dict()


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


def _layout_has_twitch_chat(layout) -> bool:
    if layout.chat_overlays:
        return True
    return any(cue.layout.chat_overlays for cue in layout.cues)


def _validate_layout(layout, *, clip_start_s: float, clip_end_s: float) -> None:
    def validate_frame(frame) -> None:
        for region in frame.overlays:
            for name, rect in (("source", region.source), ("destination", region.destination)):
                if rect.x + rect.width > 1.000001 or rect.y + rect.height > 1.000001:
                    label = region.label or region.id
                    raise HTTPException(
                        status_code=400,
                        detail=f"Layout region {label!r} {name} rectangle exceeds its frame.",
                    )
        for chat in frame.chat_overlays:
            rect = chat.destination
            if rect.x + rect.width > 1.000001 or rect.y + rect.height > 1.000001:
                raise HTTPException(
                    status_code=400,
                    detail=f"Twitch chat overlay {chat.id!r} exceeds the output frame.",
                )

    validate_frame(layout)
    previous_at: float | None = None
    for cue in layout.cues:
        if cue.at_s < clip_start_s - 0.000001 or cue.at_s > clip_end_s + 0.000001:
            raise HTTPException(
                status_code=400,
                detail="Layout cue timestamps must stay inside the clip.",
            )
        if previous_at is not None and cue.at_s <= previous_at + 0.000001:
            raise HTTPException(
                status_code=400,
                detail="Layout cue timestamps must be strictly increasing.",
            )
        validate_frame(cue.layout)
        previous_at = cue.at_s


@router.patch("/clips/{clip_id}/layout", response_model=ClipOut)
async def patch_layout(clip_id: str, payload: LayoutPatchIn) -> ClipOut:
    """Save or clear a manual 9:16/1:1 crop-and-overlay composition."""
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    existing = await asyncio.to_thread(store.get_clip_edit, clip_id)
    from ..db.models import ClipEdit

    ratio = payload.ratio or (existing.ratio if existing else "9:16")
    if payload.layout is not None:
        if ratio not in ("9:16", "1:1"):
            raise HTTPException(
                status_code=400,
                detail="Custom crop layouts are available only for 9:16 and 1:1 clips.",
            )
        if _layout_has_twitch_chat(payload.layout):
            job = await asyncio.to_thread(store.get_job, clip.job_id)
            source = await asyncio.to_thread(store.get_source, job.source_id) if job else None
            if source is None or twitch_chat.vod_id_from_url(source.url or "") is None:
                raise HTTPException(
                    status_code=400,
                    detail="Twitch chat overlays are available only for Twitch VOD projects.",
                )
        _validate_layout(payload.layout, clip_start_s=clip.start_s, clip_end_s=clip.end_s)

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


async def _render_clip_export(
    clip_id: str,
    payload: ExportRequestIn,
    *,
    destination_name: str | None = None,
) -> Export:
    """Render one clip and return the stored export record."""
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    job = await asyncio.to_thread(store.get_job, clip.job_id)
    source = await asyncio.to_thread(store.get_source, job.source_id) if job else None
    if job is None or source is None:
        raise HTTPException(status_code=404, detail="The clip's source is missing.")

    source_path = await asyncio.to_thread(storage.resolve_source_path, source)
    if not source_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Source media is missing from the configured storage folder.",
        )

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
    crop_path = await asyncio.to_thread(_crop_path_for, clip, source, payload.ratio)

    output_name = destination_name or export_module.output_filename(
        clip.title or f"clip-{clip.rank}",
        payload.ratio,
    )
    destination = paths.exports_dir() / clip.job_id / output_name

    edit = await asyncio.to_thread(store.get_clip_edit, clip_id)
    cuts = [
        (float(cut["start_s"]), float(cut["end_s"]))
        for cut in (edit.cuts if edit else [])
    ]

    request = export_module.ExportRequest(
        source=source_path,
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
        chat_assets_dir=workspace.twitch_chat_assets(clip.id),
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

    return record


@router.post("/clips/{clip_id}/export", response_model=ExportOut, status_code=201)
async def export_clip(clip_id: str, payload: ExportRequestIn) -> ExportOut:
    """Render one clip and return its download link."""
    return ExportOut.of(await _render_clip_export(clip_id, payload))


def _kept_archive_path(job_id: str) -> Path:
    return paths.exports_dir() / job_id / "kept-clips.zip"


def _write_kept_archive(
    destination: Path,
    entries: list[tuple[Path, str]],
) -> None:
    temp_path = destination.with_name(f"{destination.name}.tmp")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for source_path, archive_name in entries:
                archive.write(source_path, arcname=archive_name)
        temp_path.replace(destination)
    finally:
        temp_path.unlink(missing_ok=True)


@router.post(
    "/jobs/{job_id}/exports/kept-archive",
    response_model=ExportArchiveOut,
    status_code=201,
)
async def export_kept_archive(job_id: str) -> ExportArchiveOut:
    """Render every currently-kept clip and bundle the results into one ZIP."""
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    source = await asyncio.to_thread(store.get_source, job.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source media not found.")

    job_clips = await asyncio.to_thread(store.list_clips, job_id)
    kept = [clip for clip in job_clips if clip.status == "kept"]
    if not kept:
        raise HTTPException(status_code=400, detail="There are no kept clips to export.")

    archive_path = _kept_archive_path(job_id)
    entries: list[tuple[Path, str]] = []
    for clip in kept:
        edit = await asyncio.to_thread(store.get_clip_edit, clip.id)
        ratio = edit.ratio if edit else "9:16"
        style = edit.caption_style if edit else "bold_pop"
        base_name = export_module.output_filename(
            clip.title or f"clip-{clip.rank}",
            ratio,
        )
        unique_name = f"{clip.rank:02d}_{base_name}"
        record = await _render_clip_export(
            clip.id,
            ExportRequestIn(ratio=ratio, style=style, write_srt=False),
            destination_name=unique_name,
        )
        entries.append((Path(record.path), unique_name))

    await asyncio.to_thread(_write_kept_archive, archive_path, entries)

    filename = (
        f"{export_module.slugify_title(source.title or 'autoclip')}_kept.zip"
    )
    return ExportArchiveOut(
        filename=filename,
        size_bytes=archive_path.stat().st_size,
        clip_count=len(kept),
        download_url=f"/api/jobs/{job_id}/exports/kept-archive/download",
    )


@router.get("/jobs/{job_id}/exports/kept-archive/download")
async def download_kept_archive(job_id: str) -> FileResponse:
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    source = await asyncio.to_thread(store.get_source, job.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source media not found.")

    path = _kept_archive_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No kept-clips archive has been created yet.")

    filename = f"{export_module.slugify_title(source.title or 'autoclip')}_kept.zip"
    return FileResponse(path, media_type="application/zip", filename=filename)


@router.delete(
    "/jobs/{job_id}/clips/discarded",
    response_model=DeletedClipsOut,
)
async def delete_discarded_clips(job_id: str) -> DeletedClipsOut:
    """Permanently remove every discarded clip and its rendered export files."""
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    job_clips = await asyncio.to_thread(store.list_clips, job_id)
    discarded = [clip for clip in job_clips if clip.status == "discarded"]
    if not discarded:
        return DeletedClipsOut(deleted_ids=[], count=0)

    export_paths: set[Path] = set()
    for clip in discarded:
        for record in await asyncio.to_thread(store.list_exports, clip.id):
            export_paths.add(Path(record.path))

    deleted_ids = [clip.id for clip in discarded]
    await asyncio.to_thread(store.delete_clips, deleted_ids)

    for path in export_paths:
        if await asyncio.to_thread(store.export_path_is_referenced, str(path)):
            continue
        path.unlink(missing_ok=True)
        path.with_suffix(".srt").unlink(missing_ok=True)

    workspace = JobWorkspace(job_id)
    for clip_id in deleted_ids:
        workspace.crop_path(clip_id).unlink(missing_ok=True)
        workspace.twitch_chat(clip_id).unlink(missing_ok=True)
        shutil.rmtree(workspace.twitch_chat_assets(clip_id), ignore_errors=True)

    return DeletedClipsOut(deleted_ids=deleted_ids, count=len(deleted_ids))


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
    if source is None:
        raise HTTPException(status_code=404, detail="Source media not found.")

    source_path = await asyncio.to_thread(storage.resolve_source_path, source)
    if not source_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Source media is missing from the configured storage folder.",
        )

    media_path = source_path
    lock = _MEDIA_PREP_LOCKS.setdefault(str(source_path), asyncio.Lock())
    async with lock:
        # Remote downloads are already fast-started during ingest. Do not rewrite
        # an arbitrary uploaded MP4 on the first HTTP request: that copied the
        # entire file before the browser could receive a single byte.
        try:
            needs_proxy = await asyncio.to_thread(_preview_needs_proxy, source_path)
        except Exception as exc:
            log.warning("Could not inspect preview codec for %s: %s", source_path, exc)
            needs_proxy = False

        if needs_proxy:
            preview = JobWorkspace(job_id).preview_media
            if not preview.is_file():
                try:
                    await asyncio.to_thread(
                        ingest_module.build_browser_preview,
                        source_path,
                        preview,
                    )
                except Exception as exc:
                    log.warning(
                        "Could not create browser preview proxy for %s: %s",
                        source_path,
                        exc,
                    )
                else:
                    media_path = preview
            else:
                media_path = preview

    return FileResponse(
        media_path,
        headers={
            # The preview URL already carries a per-page cache buster. Let the
            # browser reuse byte-range responses inside that page; disabling
            # caching here causes a storm of duplicate HTTP 206 requests on
            # long videos and can keep <video> stuck in "loading".
            "Cache-Control": "private, max-age=3600",
        },
    )


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


def _crop_path_for(clip, source, ratio: str) -> CropPath:
    """Build the one static center crop used by every Reframe/export path."""
    aspect = {"9:16": (9, 16), "1:1": (1, 1), "16:9": (16, 9)}[ratio]
    return centre_crop(
        source.width or 1920,
        source.height or 1080,
        clip.end_s - clip.start_s,
        aspect_w=aspect[0],
        aspect_h=aspect[1],
    )
