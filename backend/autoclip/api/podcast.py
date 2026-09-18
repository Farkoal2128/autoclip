"""Optional podcast continuation for completed clipping jobs."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from .. import storage
from ..config import Settings, load as load_settings
from ..db import store
from ..pipeline import podcast, prepare
from ..pipeline.runner import JobWorkspace
from ..pipeline.transcript import Transcript
from ..providers import build_provider, detection_config
from .schemas import PodcastCreateIn, PodcastCutOut, PodcastOut

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["podcast"])

_active_jobs: set[str] = set()


def _podcast_out(result: podcast.PodcastResult) -> PodcastOut:
    return PodcastOut(
        job_id=result.job_id,
        filename=result.filename,
        size_bytes=result.size_bytes,
        source_duration_s=result.source_duration_s,
        output_duration_s=result.output_duration_s,
        removed_duration_s=result.removed_duration_s,
        silence_cut_count=result.silence_cut_count,
        boring_cut_count=result.boring_cut_count,
        total_cut_count=result.total_cut_count,
        provider=result.provider,
        model=result.model,
        created_at=result.created_at,
        download_url=result.download_url,
        cuts=[PodcastCutOut(**cut.__dict__) for cut in result.cuts],
    )


def _job_settings(job) -> Settings:
    """Use the job's model/export snapshot while preserving current secrets."""
    current = load_settings()
    if not job.settings:
        return current

    try:
        snapshot = Settings.model_validate(job.settings)
    except Exception:
        return current

    current.active_provider = snapshot.active_provider
    current.providers = snapshot.providers
    current.whisper = snapshot.whisper
    current.clips = snapshot.clips
    current.ingest = snapshot.ingest
    current.export = snapshot.export
    return current


async def _load_context(job_id: str):
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "done":
        raise HTTPException(
            status_code=409,
            detail="Finish the clip job before making a podcast.",
        )

    source = await asyncio.to_thread(store.get_source, job.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source media not found.")
    if not source.has_audio:
        raise HTTPException(status_code=400, detail="This source has no audio track.")

    source_path = await asyncio.to_thread(storage.resolve_source_path, source)
    if not source_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Source media is missing from the configured storage folder.",
        )

    workspace = JobWorkspace(job.id)
    if not workspace.transcript.is_file():
        raise HTTPException(
            status_code=409,
            detail="The completed job has no saved transcript to edit into a podcast.",
        )

    transcript = await asyncio.to_thread(Transcript.load, workspace.transcript)

    if workspace.silences.is_file():
        raw = json.loads(workspace.silences.read_text(encoding="utf-8"))
        silences = [prepare.Silence(**item) for item in raw]
    else:
        silence_source: Path = workspace.audio if workspace.audio.is_file() else source_path
        silences = await asyncio.to_thread(prepare.detect_silences, silence_source)

    return job, source, source_path, transcript, silences


@router.get("/{job_id}/podcast", response_model=PodcastOut)
async def get_podcast(job_id: str) -> PodcastOut:
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    result = await asyncio.to_thread(podcast.load_result, job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="No podcast has been generated for this job.")
    return _podcast_out(result)


@router.get("/{job_id}/podcast/download")
async def download_podcast(job_id: str) -> FileResponse:
    result = await asyncio.to_thread(podcast.load_result, job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Podcast file not found.")

    path = podcast.output_path(job_id)
    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=result.filename,
    )


@router.post("/{job_id}/podcast/stream")
async def make_podcast(job_id: str, payload: PodcastCreateIn) -> StreamingResponse:
    if not payload.remove_silences and not payload.remove_boring_sections:
        raise HTTPException(
            status_code=400,
            detail="Enable silence removal, boring-section removal, or both.",
        )
    if payload.silence_keep_s * 2 >= payload.silence_threshold_s:
        raise HTTPException(
            status_code=400,
            detail="Silence keep time must be less than half the silence threshold.",
        )
    if job_id in _active_jobs:
        raise HTTPException(
            status_code=409,
            detail="A podcast is already being generated for this job.",
        )

    # Reserve before the first await so two near-simultaneous clicks cannot
    # start duplicate LLM work and renders for the same job.
    _active_jobs.add(job_id)
    try:
        job, source, source_path, transcript, silences = await _load_context(job_id)
        settings = _job_settings(job)
        provider_name = job.provider or settings.active_provider
        provider = (
            build_provider(provider_name, settings)
            if payload.remove_boring_sections
            else None
        )
        config = detection_config(settings)
        config.prompt_version = "podcast_v1"
        config.temperature = 0.2
        config.max_clips = max(12, config.max_clips)

        options = podcast.PodcastOptions(
            remove_silences=payload.remove_silences,
            remove_boring_sections=payload.remove_boring_sections,
            silence_threshold_s=payload.silence_threshold_s,
            silence_keep_s=payload.silence_keep_s,
        )
    except Exception:
        _active_jobs.discard(job_id)
        raise

    async def events():
        loop = asyncio.get_running_loop()
        messages: asyncio.Queue[dict] = asyncio.Queue()

        def emit(item: dict) -> None:
            loop.call_soon_threadsafe(messages.put_nowait, item)

        def on_status(message: str) -> None:
            emit({"type": "status", "message": message})

        def on_progress(fraction: float) -> None:
            emit(
                {
                    "type": "progress",
                    "progress": round(max(0.0, min(1.0, fraction)), 4),
                }
            )

        async def run() -> None:
            try:
                result = await podcast.build_podcast(
                    job_id=job.id,
                    source=source_path,
                    source_duration_s=source.duration_s,
                    transcript=transcript,
                    silences=silences,
                    provider=provider,
                    config=config,
                    export_settings=settings.export,
                    options=options,
                    on_status=on_status,
                    on_progress=on_progress,
                )
            except podcast.PodcastError as exc:
                log.warning("Podcast generation failed for %s: %s", job_id, exc)
                await messages.put({"type": "error", "message": str(exc)})
            except Exception as exc:
                log.exception("Unexpected podcast generation failure for %s.", job_id)
                await messages.put(
                    {
                        "type": "error",
                        "message": f"Podcast generation failed: {exc}",
                    }
                )
            else:
                await messages.put(
                    {
                        "type": "done",
                        "podcast": _podcast_out(result).model_dump(mode="json"),
                    }
                )
            finally:
                _active_jobs.discard(job_id)

        task = asyncio.create_task(run())
        try:
            while True:
                item = await messages.get()
                yield json.dumps(item, separators=(",", ":")) + "\n"
                if item["type"] in ("done", "error"):
                    break
        finally:
            if not task.done():
                await asyncio.shield(task)
            else:
                await task

    return StreamingResponse(events(), media_type="application/x-ndjson")
