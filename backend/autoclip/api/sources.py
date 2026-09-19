"""Source ingestion endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse

from ..config import load as load_settings
from ..db import store
from ..pipeline import ingest
from .jobs import create_job_for_source
from .schemas import (
    RemoteIngestIn,
    RemoteIngestMessageOut,
    RemoteIngestSessionIn,
    RemoteIngestSessionOut,
    SourceOut,
    YouTubeIngestIn,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sources", tags=["sources"])

#: Uploads stream to disk in chunks rather than being buffered whole — source
#: videos routinely exceed available RAM.
UPLOAD_CHUNK = 4 * 1024 * 1024


@dataclass
class _RemoteIngestSession:
    id: str
    status: str = "running"
    progress: float | None = 0.0
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed_bytes_s: float | None = None
    total_is_estimate: bool = False
    messages: list[RemoteIngestMessageOut] = field(default_factory=list)
    source_id: str | None = None
    job_id: str | None = None
    error: str | None = None
    hint: str = ""
    task: asyncio.Task[None] | None = None


_current_remote_session: _RemoteIngestSession | None = None


def _append_session_message(session: _RemoteIngestSession, message: str) -> None:
    if session.messages and session.messages[-1].message == message:
        return
    session.messages.append(
        RemoteIngestMessageOut(
            at=datetime.now().astimezone().isoformat(timespec="seconds"),
            message=message,
        )
    )
    del session.messages[:-40]


def _session_out(session: _RemoteIngestSession) -> RemoteIngestSessionOut:
    return RemoteIngestSessionOut(
        id=session.id,
        status=session.status,
        progress=session.progress,
        downloaded_bytes=session.downloaded_bytes,
        total_bytes=session.total_bytes,
        speed_bytes_s=session.speed_bytes_s,
        total_is_estimate=session.total_is_estimate,
        messages=list(session.messages),
        source_id=session.source_id,
        job_id=session.job_id,
        error=session.error,
        hint=session.hint,
    )


def _apply_download_progress(
    session: _RemoteIngestSession,
    progress: ingest.DownloadProgress,
) -> None:
    session.progress = progress.progress
    session.downloaded_bytes = progress.downloaded_bytes
    session.total_bytes = progress.total_bytes
    session.speed_bytes_s = progress.speed_bytes_s
    session.total_is_estimate = progress.total_is_estimate


@router.get("", response_model=list[SourceOut])
async def list_sources(limit: int = 50) -> list[SourceOut]:
    sources = await asyncio.to_thread(store.list_sources, limit)
    return [SourceOut.of(s) for s in sources]


@router.get("/{source_id}", response_model=SourceOut)
async def get_source(source_id: str) -> SourceOut:
    source = await asyncio.to_thread(store.get_source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found.")
    return SourceOut.of(source)


async def _ingest_remote(payload: RemoteIngestIn, *, youtube_only: bool = False) -> SourceOut:
    if youtube_only:
        if not ingest.is_youtube_url(payload.url):
            raise HTTPException(status_code=400, detail="That is not a YouTube URL.")
    elif not ingest.is_supported_url(payload.url):
        raise HTTPException(
            status_code=400,
            detail="Paste a YouTube video URL or a Twitch VOD URL such as twitch.tv/videos/123.",
        )

    settings = load_settings().ingest
    if payload.cookies_from_browser is not None:
        settings = settings.model_copy(
            update={"cookies_from_browser": payload.cookies_from_browser}
        )

    try:
        source = await asyncio.to_thread(ingest.ingest_url, payload.url, settings)
    except ingest.IngestError as exc:
        # 422 rather than 500: the request was well-formed, but the content
        # can't be fetched, and the hint tells the user what to change.
        raise HTTPException(
            status_code=422, detail={"message": str(exc), "hint": exc.hint}
        ) from exc

    await asyncio.to_thread(store.create_source, source)
    return SourceOut.of(source)


@router.post("/url", response_model=SourceOut, status_code=201)
async def ingest_url(payload: RemoteIngestIn) -> SourceOut:
    """Download a supported YouTube video or Twitch VOD and register it."""
    return await _ingest_remote(payload)


@router.post("/url/sessions", response_model=RemoteIngestSessionOut, status_code=202)
async def start_remote_ingest_session(payload: RemoteIngestSessionIn) -> RemoteIngestSessionOut:
    """Start a remote download that survives browser navigation and reconnects by polling."""
    global _current_remote_session

    if not ingest.is_supported_url(payload.url):
        raise HTTPException(
            status_code=400,
            detail="Paste a YouTube video URL or a Twitch VOD URL such as twitch.tv/videos/123.",
        )

    if _current_remote_session is not None and _current_remote_session.status == "running":
        raise HTTPException(
            status_code=409,
            detail="A remote video is already downloading. Open New to view its progress.",
        )

    settings = load_settings().ingest
    if payload.cookies_from_browser is not None:
        settings = settings.model_copy(
            update={"cookies_from_browser": payload.cookies_from_browser}
        )

    session = _RemoteIngestSession(id=uuid4().hex)
    _append_session_message(session, "Starting remote video fetch")
    _current_remote_session = session
    loop = asyncio.get_running_loop()

    def on_status(message: str) -> None:
        loop.call_soon_threadsafe(_append_session_message, session, message)

    def on_progress(fraction: float) -> None:
        loop.call_soon_threadsafe(setattr, session, "progress", round(fraction, 4))

    def on_download_progress(progress: ingest.DownloadProgress) -> None:
        loop.call_soon_threadsafe(_apply_download_progress, session, progress)

    async def run() -> None:
        try:
            source = await asyncio.to_thread(
                ingest.ingest_url,
                payload.url,
                settings,
                on_progress=on_progress,
                on_status=on_status,
                on_download_progress=on_download_progress,
            )
            await asyncio.to_thread(store.create_source, source)
            session.source_id = source.id
            _append_session_message(session, "Source registered; creating processing job")
            job = await create_job_for_source(source, payload.settings)
        except ingest.IngestCancelled:
            session.status = "error"
            session.error = "Download cancelled because AutoClip is shutting down."
            _append_session_message(session, session.error)
        except ingest.IngestError as exc:
            session.status = "error"
            session.error = str(exc.args[0]) if exc.args else str(exc)
            session.hint = exc.hint
            _append_session_message(session, session.error)
        except HTTPException as exc:
            session.status = "error"
            detail = exc.detail
            session.error = (
                detail
                if isinstance(detail, str)
                else str(detail.get("message", detail))
                if isinstance(detail, dict)
                else str(detail)
            )
            _append_session_message(session, session.error)
        except Exception as exc:
            log.exception("Remote ingest session failed.")
            session.status = "error"
            session.error = f"Remote download failed: {exc}"
            _append_session_message(session, session.error)
        else:
            session.job_id = job.id
            session.progress = 1.0
            session.status = "done"
            _append_session_message(session, "Job queued; pipeline is ready")

    session.task = asyncio.create_task(run())
    return _session_out(session)


@router.get("/url/sessions/current", response_model=RemoteIngestSessionOut | None)
async def current_remote_ingest_session() -> RemoteIngestSessionOut | None:
    """Return the current remote ingest so a reloaded UI can reconnect."""
    if _current_remote_session is None:
        return None
    return _session_out(_current_remote_session)


@router.delete("/url/sessions/{session_id}", status_code=204)
async def clear_remote_ingest_session(session_id: str) -> Response:
    """Acknowledge a completed/error ingest after its UI notification is handled."""
    global _current_remote_session

    session = _current_remote_session
    if session is None or session.id != session_id:
        return Response(status_code=204)
    if session.status == "running":
        raise HTTPException(status_code=409, detail="The download is still running.")

    _current_remote_session = None
    return Response(status_code=204)


@router.post("/url/stream")
async def ingest_url_stream(payload: RemoteIngestIn) -> StreamingResponse:
    """Stream human-readable yt-dlp activity while a remote source downloads."""
    if not ingest.is_supported_url(payload.url):
        raise HTTPException(
            status_code=400,
            detail="Paste a YouTube video URL or a Twitch VOD URL such as twitch.tv/videos/123.",
        )

    settings = load_settings().ingest
    if payload.cookies_from_browser is not None:
        settings = settings.model_copy(
            update={"cookies_from_browser": payload.cookies_from_browser}
        )

    async def events():
        loop = asyncio.get_running_loop()
        messages: asyncio.Queue[dict] = asyncio.Queue()

        def emit(item: dict) -> None:
            loop.call_soon_threadsafe(messages.put_nowait, item)

        def on_status(message: str) -> None:
            emit({"type": "status", "message": message})

        def on_progress(fraction: float) -> None:
            emit({"type": "progress", "progress": round(fraction, 4)})

        def on_download_progress(progress: ingest.DownloadProgress) -> None:
            emit(
                {
                    "type": "progress",
                    "progress": (
                        round(progress.progress, 4)
                        if progress.progress is not None
                        else None
                    ),
                    "downloaded_bytes": progress.downloaded_bytes,
                    "total_bytes": progress.total_bytes,
                    "speed_bytes_s": progress.speed_bytes_s,
                    "total_is_estimate": progress.total_is_estimate,
                }
            )

        async def download() -> None:
            try:
                source = await asyncio.to_thread(
                    ingest.ingest_url,
                    payload.url,
                    settings,
                    on_progress=on_progress,
                    on_status=on_status,
                    on_download_progress=on_download_progress,
                )
                await asyncio.to_thread(store.create_source, source)
            except ingest.IngestError as exc:
                message = str(exc.args[0]) if exc.args else str(exc)
                await messages.put(
                    {
                        "type": "error",
                        "message": message,
                        "hint": exc.hint,
                    }
                )
            except Exception as exc:
                log.exception("Remote ingest stream failed.")
                await messages.put(
                    {
                        "type": "error",
                        "message": f"Remote download failed: {exc}",
                        "hint": "",
                    }
                )
            else:
                await messages.put(
                    {
                        "type": "done",
                        "source": SourceOut.of(source).model_dump(mode="json"),
                    }
                )

        task = asyncio.create_task(download())
        while True:
            item = await messages.get()
            yield json.dumps(item, separators=(",", ":")) + "\n"
            if item["type"] in ("done", "error"):
                break
        await task

    return StreamingResponse(events(), media_type="application/x-ndjson")


@router.post("/youtube", response_model=SourceOut, status_code=201)
async def ingest_youtube(payload: YouTubeIngestIn) -> SourceOut:
    """Legacy YouTube-only endpoint."""
    return await _ingest_remote(payload, youtube_only=True)


@router.post("/upload", response_model=SourceOut, status_code=201)
async def upload_source(file: UploadFile = File(...)) -> SourceOut:
    """Accept a media upload and register it as a source."""
    filename = Path(file.filename or "upload")
    suffix = filename.suffix.lower()
    log.info("Receiving local upload %s.", filename.name)
    if suffix not in ingest.ACCEPTED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail={
                "message": f"{suffix or 'This file'} is not a supported format.",
                "hint": f"Accepted: {', '.join(sorted(ingest.ACCEPTED_SUFFIXES))}",
            },
        )

    # Stage to a temp file first so a failed or abandoned upload never leaves a
    # half-written file in the media store.
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as staging:
        staging_path = Path(staging.name)
        try:
            while chunk := await file.read(UPLOAD_CHUNK):
                staging.write(chunk)
        except Exception as exc:
            staging_path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail="Upload failed.") from exc

    log.info("Upload received; validating and storing %s.", filename.name)
    try:
        source = await asyncio.to_thread(
            ingest.ingest_file, staging_path, move=True, title=filename.stem
        )
    except ingest.IngestError as exc:
        staging_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=422, detail={"message": str(exc), "hint": exc.hint}
        ) from exc
    finally:
        shutil.rmtree(staging_path.parent / staging_path.name, ignore_errors=True)

    await asyncio.to_thread(store.create_source, source)
    log.info("Local upload %s is ready as source %s.", filename.name, source.id)
    return SourceOut.of(source)
