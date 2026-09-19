"""Settings, secrets, provider health, and system status."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
from uuid import uuid4
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from .. import config, desktop, paths, server_control, storage, system, updater
from ..jobs.queue import queue
from ..pipeline import ingest
from ..providers import PROVIDERS, build_provider
from ..providers.base import ProviderStatus
from .schemas import (
    DesktopShortcutOut,
    FolderChoiceOut,
    LayoutPresetCreateIn,
    LayoutPresetOut,
    ProviderStatusOut,
    SecretIn,
    SettingsIn,
    SettingsOut,
    StorageMoveIn,
    StorageOut,
    SystemOut,
    UpdateResultOut,
    UpdateStartOut,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["settings"])


def _open_folder(path: Path) -> None:
    """Open a trusted AutoClip directory in the platform file manager."""
    target = str(path.resolve())
    if sys.platform == "win32":
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise OSError("Windows folder opening is unavailable.")
        startfile(target)
        return

    command = ["open", target] if sys.platform == "darwin" else ["xdg-open", target]
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _storage_out() -> StorageOut:
    current = storage.status()
    return StorageOut(
        path=str(current.path),
        control_path=str(current.control_path),
        custom=current.custom,
        managed_by_env=current.managed_by_env,
        free_bytes=current.free_bytes,
        total_bytes=current.total_bytes,
    )


def _desktop_shortcut_out() -> DesktopShortcutOut:
    current = desktop.shortcut_status()
    return DesktopShortcutOut(
        supported=current.supported,
        exists=current.exists,
        path=str(current.path) if current.path else None,
    )


def _choose_directory(initial: Path) -> Path | None:
    """Open a native folder chooser where Tk is available.

    The text field in Settings remains the fallback for headless/minimal installs.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - platform packaging dependent
        raise OSError("A native folder picker is not available; type the path manually.") from exc

    root = tk.Tk()
    root.withdraw()
    try:
        with contextlib.suppress(Exception):
            root.attributes("-topmost", True)
        selected = filedialog.askdirectory(initialdir=str(initial), mustexist=False)
    finally:
        root.destroy()

    return Path(selected).expanduser().resolve() if selected else None


def _settings_out(settings: config.Settings) -> SettingsOut:
    payload = settings.model_dump(mode="json")
    return SettingsOut(
        active_provider=payload["active_provider"],
        providers=payload["providers"],
        whisper=payload["whisper"],
        clips=payload["clips"],
        ingest=payload["ingest"],
        export=payload["export"],
        insecure_secret_storage=payload["insecure_secret_storage"],
        keys_present={
            name: config.get_secret(name, settings) is not None for name in config.KEYED_PROVIDERS
        }
        | {config.HF_TOKEN_KEY: config.get_secret(config.HF_TOKEN_KEY, settings) is not None},
    )





def _validate_preset_layout(layout) -> None:
    for region in layout.overlays:
        for name, rect in (("source", region.source), ("destination", region.destination)):
            if rect.x + rect.width > 1.000001 or rect.y + rect.height > 1.000001:
                label = region.label or region.id
                raise HTTPException(
                    status_code=400,
                    detail=f"Layout region {label!r} {name} rectangle exceeds its frame.",
                )


@router.get("/layout-presets", response_model=list[LayoutPresetOut])
async def list_layout_presets() -> list[LayoutPresetOut]:
    settings = config.load()
    return [LayoutPresetOut(**preset.model_dump()) for preset in settings.layout_presets]


@router.post("/layout-presets", response_model=LayoutPresetOut, status_code=201)
async def create_layout_preset(payload: LayoutPresetCreateIn) -> LayoutPresetOut:
    settings = config.load()
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Preset name cannot be empty.")
    if any(preset.name.casefold() == name.casefold() for preset in settings.layout_presets):
        raise HTTPException(
            status_code=409,
            detail="A layout preset with that name already exists.",
        )

    _validate_preset_layout(payload.layout)
    preset = config.LayoutPresetSettings(
        id=uuid4().hex,
        name=name,
        ratio=payload.ratio,
        layout=payload.layout.model_dump(),
    )
    settings.layout_presets.append(preset)
    config.save(settings)
    return LayoutPresetOut(**preset.model_dump())


@router.delete("/layout-presets/{preset_id}", status_code=204)
async def delete_layout_preset(preset_id: str) -> Response:
    settings = config.load()
    kept = [preset for preset in settings.layout_presets if preset.id != preset_id]
    if len(kept) == len(settings.layout_presets):
        raise HTTPException(status_code=404, detail="Layout preset not found.")
    settings.layout_presets = kept
    config.save(settings)
    return Response(status_code=204)


@router.get("/settings", response_model=SettingsOut)
async def get_settings() -> SettingsOut:
    return _settings_out(config.load())


@router.put("/settings", response_model=SettingsOut)
async def put_settings(payload: SettingsIn) -> SettingsOut:
    """Merge a partial settings update and persist it."""
    settings = config.load()
    updates = payload.model_dump(exclude_none=True)

    if "active_provider" in updates:
        if updates["active_provider"] not in PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown provider. Available: {', '.join(PROVIDERS)}",
            )
        settings.active_provider = updates["active_provider"]

    for section in ("whisper", "clips", "ingest", "export"):
        if section in updates:
            current = getattr(settings, section)
            try:
                setattr(
                    settings,
                    section,
                    current.model_copy(update=updates[section]).model_validate(
                        current.model_dump() | updates[section]
                    ),
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail=f"Invalid {section} settings: {exc}"
                ) from exc

    if "providers" in updates:
        for name, values in updates["providers"].items():
            provider_settings = settings.provider(name)
            settings.providers[name] = provider_settings.model_copy(update=values)

    if settings.clips.min_duration_s >= settings.clips.max_duration_s:
        raise HTTPException(
            status_code=400, detail="Minimum clip length must be below the maximum."
        )

    config.save(settings)
    return _settings_out(settings)


@router.put("/settings/secrets", status_code=204)
async def put_secret(payload: SecretIn) -> None:
    """Store an API key or token. Values are write-only — never read back."""
    valid = (*config.KEYED_PROVIDERS, config.HF_TOKEN_KEY)
    if payload.key not in valid:
        raise HTTPException(
            status_code=400, detail=f"Unknown secret. Expected one of: {', '.join(valid)}"
        )
    if not payload.value.strip():
        raise HTTPException(status_code=400, detail="The value cannot be empty.")

    config.set_secret(payload.key, payload.value.strip())


@router.delete("/settings/secrets/{key}", status_code=204)
async def delete_secret(key: str) -> None:
    config.delete_secret(key)


@router.get("/providers/status", response_model=list[ProviderStatusOut])
async def providers_status() -> list[ProviderStatusOut]:
    """Live health for every provider, checked concurrently."""
    settings = config.load()

    async def check(name: str) -> ProviderStatusOut:
        provider_cls = PROVIDERS[name]
        has_key = (
            config.get_secret(name, settings) is not None if provider_cls.requires_key else True
        )
        try:
            provider = build_provider(name, settings)
            status: ProviderStatus = await provider.health_check()
        except Exception as exc:
            status = ProviderStatus(name=name, available=False, detail=str(exc)[:200])

        return ProviderStatusOut(
            name=name,
            available=status.available,
            detail=status.detail,
            models=status.models,
            requires_key=provider_cls.requires_key,
            has_key=has_key,
        )

    return list(await asyncio.gather(*(check(name) for name in PROVIDERS)))


@router.get("/system", response_model=SystemOut)
async def system_status() -> SystemOut:
    report = await asyncio.to_thread(system.refresh)
    return SystemOut(
        ready=report.ready,
        python_version=report.python_version,
        platform=report.platform,
        ffmpeg_version=report.ffmpeg.version,
        has_libass=report.ffmpeg.has_libass,
        nvenc_works=report.ffmpeg.nvenc_works,
        accel=report.gpu.accel,
        gpu_name=report.gpu.name,
        compute_type=report.gpu.compute_type,
        diarization_available=report.deps.whisperx,
    )


@router.post("/system/open-location/{location}", status_code=204)
async def open_location(location: str) -> Response:
    """Open AutoClip's data folder or source/install folder in the OS file manager."""
    if location == "data":
        paths.ensure_layout()
        target = paths.data_root()
    elif location == "install":
        target = paths.install_dir()
    else:
        raise HTTPException(status_code=404, detail="Unknown AutoClip location.")

    try:
        await asyncio.to_thread(_open_folder, target)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not open {target}: {exc}",
        ) from exc

    return Response(status_code=204)


def _is_loopback_request(request: Request) -> bool:
    if request.client is None:
        return False
    return request.client.host in {"127.0.0.1", "::1", "testclient"}


@router.post("/system/update", response_model=UpdateStartOut, status_code=202)
async def update_autoclip(
    request: Request, background_tasks: BackgroundTasks
) -> UpdateStartOut:
    """Shut down, fast-forward main, rebuild AutoClip, and relaunch it."""
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="AutoClip can only update from this machine.")

    queue_status = queue.status()
    if queue_status.running_job_id is not None or queue_status.queued:
        raise HTTPException(
            status_code=409,
            detail="Finish or cancel queued/running jobs before updating AutoClip.",
        )

    if ingest.active_download_count():
        raise HTTPException(
            status_code=409,
            detail="Finish or cancel the active video download before updating AutoClip.",
        )

    if not server_control.shutdown_available():
        raise HTTPException(
            status_code=503,
            detail="In-app update is unavailable for this server mode.",
        )

    try:
        token = await asyncio.to_thread(updater.launch_detached, os.getpid())
    except updater.UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    background_tasks.add_task(server_control.request_shutdown)
    return UpdateStartOut(status="updating", token=token)


@router.get(
    "/system/update-result/{token}",
    response_model=UpdateResultOut | None,
)
async def get_update_result(token: str) -> UpdateResultOut | None:
    payload = await asyncio.to_thread(updater.read_result, token)
    return UpdateResultOut(**payload) if payload is not None else None


@router.delete("/system/update-result/{token}", status_code=204)
async def delete_update_result(token: str) -> Response:
    await asyncio.to_thread(updater.clear_result, token)
    return Response(status_code=204)


@router.post("/system/shutdown", status_code=202)
async def shutdown_server(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, str]:
    """Gracefully stop the local AutoClip server after this response is sent."""
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="AutoClip can only be quit from this machine.")

    queue_status = queue.status()
    if queue_status.running_job_id is not None or queue_status.queued:
        raise HTTPException(
            status_code=409,
            detail="Finish or cancel queued/running jobs before quitting AutoClip.",
        )

    if not server_control.shutdown_available():
        raise HTTPException(
            status_code=503,
            detail="Graceful quit is unavailable for this server mode.",
        )

    active_downloads = ingest.cancel_active_downloads()
    if active_downloads:
        deadline = asyncio.get_running_loop().time() + 6.5
        while ingest.active_download_count() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        if ingest.active_download_count():
            raise HTTPException(
                status_code=503,
                detail="The active download did not stop cleanly. Try Quit AutoClip again.",
            )

    background_tasks.add_task(server_control.request_shutdown)
    return {"status": "stopping"}


@router.get("/system/desktop-shortcut", response_model=DesktopShortcutOut)
async def get_desktop_shortcut() -> DesktopShortcutOut:
    return await asyncio.to_thread(_desktop_shortcut_out)


@router.post("/system/desktop-shortcut", response_model=DesktopShortcutOut)
async def create_desktop_shortcut() -> DesktopShortcutOut:
    try:
        await asyncio.to_thread(desktop.create_shortcut)
    except desktop.DesktopShortcutError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    return await asyncio.to_thread(_desktop_shortcut_out)


@router.delete("/system/desktop-shortcut", status_code=204)
async def delete_desktop_shortcut() -> Response:
    try:
        await asyncio.to_thread(desktop.remove_shortcut)
    except desktop.DesktopShortcutError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    return Response(status_code=204)


@router.get("/storage", response_model=StorageOut)
async def get_storage() -> StorageOut:
    return await asyncio.to_thread(_storage_out)


@router.post("/storage/browse", response_model=FolderChoiceOut)
async def browse_storage() -> FolderChoiceOut:
    try:
        selected = await asyncio.to_thread(_choose_directory, paths.data_root())
    except OSError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    return FolderChoiceOut(path=str(selected) if selected else None)


def _validate_storage_move(payload: StorageMoveIn) -> str:
    target = payload.path.strip()
    if not target:
        raise HTTPException(status_code=400, detail="Storage path cannot be empty.")

    queue_status = queue.status()
    if queue_status.running_job_id is not None or queue_status.queued:
        raise HTTPException(
            status_code=409,
            detail="Cancel or finish queued/running jobs before moving AutoClip storage.",
        )
    return target


@router.put("/storage", response_model=StorageOut)
async def move_storage(payload: StorageMoveIn) -> StorageOut:
    """Move media/work/exports to a different local folder."""
    target = _validate_storage_move(payload)

    try:
        await asyncio.to_thread(storage.relocate, target)
    except storage.StorageError as exc:
        log.warning("Storage relocation failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return await asyncio.to_thread(_storage_out)


@router.post("/storage/stream")
async def move_storage_stream(payload: StorageMoveIn) -> StreamingResponse:
    """Stream storage relocation milestones and byte-copy progress."""
    target = _validate_storage_move(payload)

    async def events():
        loop = asyncio.get_running_loop()
        messages: asyncio.Queue[dict] = asyncio.Queue()

        def emit(item: dict) -> None:
            loop.call_soon_threadsafe(messages.put_nowait, item)

        def on_status(message: str) -> None:
            emit({"type": "status", "message": message})

        def on_progress(fraction: float, folder: str | None) -> None:
            emit(
                {
                    "type": "progress",
                    "progress": round(fraction, 4),
                    "folder": folder,
                }
            )

        async def move() -> None:
            try:
                await asyncio.to_thread(
                    storage.relocate,
                    target,
                    on_status=on_status,
                    on_progress=on_progress,
                )
            except storage.StorageError as exc:
                log.warning("Storage relocation failed: %s", exc)
                await messages.put({"type": "error", "message": str(exc)})
            except Exception as exc:
                log.exception("Storage relocation stream failed.")
                await messages.put(
                    {
                        "type": "error",
                        "message": f"Storage relocation failed: {exc}",
                    }
                )
            else:
                current = await asyncio.to_thread(_storage_out)
                await messages.put(
                    {
                        "type": "done",
                        "storage": current.model_dump(mode="json"),
                    }
                )

        task = asyncio.create_task(move())
        while True:
            item = await messages.get()
            yield json.dumps(item, separators=(",", ":")) + "\n"
            if item["type"] in ("done", "error"):
                break
        await task

    return StreamingResponse(events(), media_type="application/x-ndjson")
