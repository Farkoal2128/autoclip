"""Source ingestion endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from ..config import load as load_settings
from ..db import store
from ..pipeline import ingest
from .schemas import RemoteIngestIn, SourceOut, YouTubeIngestIn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sources", tags=["sources"])

#: Uploads stream to disk in chunks rather than being buffered whole — source
#: videos routinely exceed available RAM.
UPLOAD_CHUNK = 4 * 1024 * 1024


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
