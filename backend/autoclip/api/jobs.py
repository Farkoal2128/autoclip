"""Job lifecycle and the SSE progress stream."""

from __future__ import annotations

import asyncio
import logging
import shutil

from fastapi import APIRouter, HTTPException, Request, Response
from sse_starlette.sse import EventSourceResponse

from .. import paths, reuse
from ..config import load as load_settings
from ..db import store
from ..db.models import Job, Transcript as TranscriptRow, new_id
from ..jobs.events import broker
from ..jobs.queue import queue
from .schemas import JobCreateIn, JobOut, JobSettingsIn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _apply_overrides(settings, overrides: JobSettingsIn):
    """Layer per-job overrides on top of saved settings."""
    merged = settings.model_copy(deep=True)

    if overrides.provider:
        merged.active_provider = overrides.provider
    if overrides.whisper_model:
        merged.whisper.model = overrides.whisper_model
    if overrides.language is not None:
        merged.whisper.language = overrides.language
    if overrides.diarization is not None:
        merged.whisper.diarization = overrides.diarization
    if overrides.min_duration_s is not None:
        merged.clips.min_duration_s = overrides.min_duration_s
    if overrides.max_duration_s is not None:
        merged.clips.max_duration_s = overrides.max_duration_s
    if overrides.max_clips is not None:
        merged.clips.max_clips = overrides.max_clips
    if overrides.caption_style:
        merged.export.caption_style = overrides.caption_style
    if overrides.ratio:
        merged.export.ratio = overrides.ratio
    if overrides.reframe_mode:
        merged.export.reframe_mode = overrides.reframe_mode

    return merged


@router.post("", response_model=JobOut, status_code=201)
async def create_job(payload: JobCreateIn) -> JobOut:
    source = await asyncio.to_thread(store.get_source, payload.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found.")

    settings = _apply_overrides(load_settings(), payload.settings)
    if settings.clips.min_duration_s >= settings.clips.max_duration_s:
        raise HTTPException(
            status_code=400, detail="Minimum clip length must be below the maximum."
        )

    job = Job(
        id=new_id(),
        source_id=source.id,
        provider=settings.active_provider,
        settings=settings.model_dump(mode="json"),
    )
    await asyncio.to_thread(store.create_job, job)
    queue.notify()

    return JobOut.of(job, source)


@router.post("/{job_id}/find-more", response_model=JobOut, status_code=201)
async def find_more_clips(job_id: str) -> JobOut:
    """Create a new highlight pass while reusing the original analysis artifacts."""
    parent = await asyncio.to_thread(store.get_job, job_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if parent.status != "done":
        raise HTTPException(
            status_code=409,
            detail="Finish this project before starting another highlight pass.",
        )

    source = await asyncio.to_thread(store.get_source, parent.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source media not found.")

    transcript_row = await asyncio.to_thread(store.get_transcript, parent.id)
    if transcript_row is None:
        raise HTTPException(
            status_code=409,
            detail="This project has no reusable transcript.",
        )

    current = load_settings()
    parent_export = parent.settings.get("export", {}) if isinstance(parent.settings, dict) else {}
    parent_reframe_mode = parent_export.get("reframe_mode")
    if parent_reframe_mode in ("smart", "fast"):
        current.export.reframe_mode = parent_reframe_mode

    child_id = new_id()
    child = Job(
        id=child_id,
        source_id=parent.source_id,
        provider=current.active_provider,
        settings=reuse.build_rerun_settings(
            parent,
            current.model_dump(mode="json"),
            [
                (clip.start_word, clip.end_word)
                for clip in await asyncio.to_thread(store.list_clips, parent.id)
            ],
        ),
    )

    try:
        await asyncio.to_thread(reuse.seed_analysis_artifacts, parent.id, child.id)
    except reuse.ArtifactReuseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    created = False
    try:
        await asyncio.to_thread(store.create_job, child)
        created = True
        await asyncio.to_thread(
            store.upsert_transcript,
            TranscriptRow(
                job_id=child.id,
                json_path=str(paths.job_work_dir(child.id) / "transcript.json"),
                language=transcript_row.language,
                model=transcript_row.model,
                has_diarization=transcript_row.has_diarization,
                word_count=transcript_row.word_count,
                source=transcript_row.source,
                created_at=transcript_row.created_at,
            ),
        )
    except Exception:
        if created:
            await asyncio.to_thread(store.delete_job, child.id)
        await asyncio.to_thread(
            shutil.rmtree,
            paths.job_work_dir(child.id),
            ignore_errors=True,
        )
        raise

    queue.notify()
    return JobOut.of(child, source)


@router.get("", response_model=list[JobOut])
async def list_jobs(limit: int = 25) -> list[JobOut]:
    jobs = await asyncio.to_thread(store.list_jobs, limit)
    out: list[JobOut] = []
    for job in jobs:
        source = await asyncio.to_thread(store.get_source, job.source_id)
        out.append(JobOut.of(job, source))
    return out


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: str) -> JobOut:
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    source = await asyncio.to_thread(store.get_source, job.source_id)
    return JobOut.of(job, source)


@router.get("/{job_id}/events")
async def job_events(job_id: str, request: Request) -> EventSourceResponse:
    """Stream progress for a job as Server-Sent Events."""
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    async def stream():
        # Send the current state immediately: a client connecting mid-job (or
        # reconnecting) must not wait for the next progress tick to render.
        current = await asyncio.to_thread(store.get_job, job_id)
        if current is not None:
            yield {
                "event": "snapshot",
                "data": JobOut.of(current).model_dump_json(),
            }
            if current.status in ("done", "failed", "cancelled"):
                return

        async for event in broker.subscribe(job_id):
            if await request.is_disconnected():
                break
            yield {"event": event.type, "data": event.to_sse().split("data: ", 1)[-1].strip()}

    return EventSourceResponse(stream())


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str) -> JobOut:
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    if not queue.cancel(job_id):
        raise HTTPException(
            status_code=409, detail=f"Job is already {job.status}; nothing to cancel."
        )

    updated = await asyncio.to_thread(store.get_job, job_id)
    return JobOut.of(updated or job)


@router.post("/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: str) -> JobOut:
    """Requeue a failed or cancelled job.

    Completed stages left their artifacts in ``work/{job_id}/``, so the retry
    resumes at the stage that failed rather than starting over.
    """
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status not in ("failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=f"Only failed or cancelled jobs can be retried (this one is {job.status}).",
        )

    await asyncio.to_thread(store.update_job, job_id, status="queued", error=None, progress=0.0)
    queue.notify()

    updated = await asyncio.to_thread(store.get_job, job_id)
    return JobOut.of(updated or job)


@router.delete("/{job_id}", status_code=204)
async def delete_job(job_id: str) -> Response:
    """Remove a finished project and its on-disk artifacts.

    Running and queued jobs cannot be removed because the worker may still be
    reading or writing their files. A source media directory is removed only
    when no other job still references that source.
    """
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status in ("queued", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"Stop this {job.status} job before removing it.",
        )

    deleted, orphaned_source_id = await asyncio.to_thread(store.delete_job, job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Job not found.")

    cleanup = [
        paths.job_work_dir(job_id),
        paths.exports_dir() / job_id,
    ]
    if orphaned_source_id is not None:
        cleanup.append(paths.source_media_dir(orphaned_source_id))

    for directory in cleanup:
        try:
            await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            # The database record is already gone; leave a warning rather than
            # turning a successful removal into a misleading API failure.
            log.warning("Could not remove project artifact directory %s: %s", directory, exc)

    return Response(status_code=204)


@router.get("/{job_id}/clips")
async def job_clips(job_id: str):
    from .clips import list_clips_for_job

    return await list_clips_for_job(job_id)
