"""Ingestion — YouTube URLs and local file uploads become source records.

Both paths converge on a validated :class:`~autoclip.db.models.Source` with a
file on disk and probed metadata.

A note on YouTube: as of 2026 most anonymous downloads hit a bot check, and
proof-of-origin tokens no longer clear it reliably — browser cookies do. So
:class:`IngestError` distinguishes that specific failure and tells the user how
to fix it, rather than surfacing a raw yt-dlp traceback.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import paths
from ..config import IngestSettings
from ..db.models import Source, new_id
from . import ffmpeg

log = logging.getLogger(__name__)

#: Container and audio formats we accept for upload.
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
ACCEPTED_SUFFIXES = VIDEO_SUFFIXES | AUDIO_SUFFIXES

LEGACY_YTDLP_FORMAT = (
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]"
)
BROWSER_YTDLP_FORMAT = (
    "bestvideo[height<=1080][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
    "best[height<=1080][vcodec^=avc1][ext=mp4]/best[height<=1080][ext=mp4]"
)

_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "www.youtube.com", "m.youtube.com")
_TWITCH_HOSTS = ("twitch.tv", "www.twitch.tv", "m.twitch.tv")
_TWITCH_VOD_PATH = re.compile(r"^/(?:videos/\d+|[^/]+/(?:v|video)/\d+)/?$")

#: yt-dlp error fragments that mean "YouTube wants proof you're a human".
_BOT_CHECK_MARKERS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "confirm you are not a bot",
    "this content isn't available",
    "player response",
)


class IngestError(RuntimeError):
    """Ingestion failed in a way worth explaining to the user."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base}\n\n{self.hint}" if self.hint else base


@dataclass(frozen=True)
class DownloadProgress:
    """Structured yt-dlp transfer metrics for the ingest UI."""

    progress: float | None
    downloaded_bytes: int | None
    total_bytes: int | None
    speed_bytes_s: float | None
    total_is_estimate: bool = False


def _parsed_url(url: str):
    from urllib.parse import urlparse

    try:
        return urlparse(url)
    except ValueError:
        return None


def is_youtube_url(url: str) -> bool:
    parsed = _parsed_url(url)
    return bool(parsed and (parsed.hostname or "").lower() in _YOUTUBE_HOSTS)


def is_twitch_vod_url(url: str) -> bool:
    """Return True for Twitch VOD URLs supported by yt-dlp.

    Live channel pages are deliberately excluded: AutoClip expects a finite
    source with a stable duration before transcription begins.
    """
    parsed = _parsed_url(url)
    if parsed is None or (parsed.hostname or "").lower() not in _TWITCH_HOSTS:
        return False
    return bool(_TWITCH_VOD_PATH.match(parsed.path or ""))


def is_supported_url(url: str) -> bool:
    """Remote URLs AutoClip intentionally accepts in the ingest UI."""
    return is_youtube_url(url) or is_twitch_vod_url(url)


def _platform_name(url: str) -> str:
    if is_twitch_vod_url(url):
        return "Twitch"
    return "YouTube"


def slugify(text: str, *, max_length: int = 60) -> str:
    """Turn a title into a filesystem- and URL-safe slug."""
    slug = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug[:max_length] or "clip"


# --------------------------------------------------------------------------
# Remote URLs (YouTube + Twitch VODs)
# --------------------------------------------------------------------------


def ingest_url(
    url: str,
    settings: IngestSettings | None = None,
    *,
    on_progress: Callable[[float], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    on_download_progress: Callable[[DownloadProgress], None] | None = None,
) -> Source:
    """Download a supported remote video and return a validated source record.

    yt-dlp supports far more sites, but AutoClip deliberately exposes only the
    sources we test and can give useful error messages for: YouTube videos and
    finite Twitch VODs.

    Preconditions:
        url points at content the user owns or has the rights to process.
    """
    if not is_supported_url(url):
        raise IngestError("Only YouTube videos and Twitch VOD URLs are supported.")
    import yt_dlp

    settings = settings or IngestSettings()
    platform = _platform_name(url)
    source_id = new_id()
    target_dir = paths.source_media_dir(source_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    download_announced = False
    component_totals: dict[str, int] = {}
    component_downloaded: dict[str, int] = {}
    expected_total: int | None = None
    expected_total_is_estimate = False

    def notify(message: str) -> None:
        log.info("%s ingest: %s", platform, message)
        if on_status:
            on_status(message)

    def hook(status: dict) -> None:
        nonlocal download_announced, expected_total, expected_total_is_estimate
        state = status.get("status")
        key = str(
            status.get("filename")
            or status.get("tmpfilename")
            or (status.get("info_dict") or {}).get("format_id")
            or "download"
        )

        exact_total = _positive_int(status.get("total_bytes"))
        estimated_total = _positive_int(status.get("total_bytes_estimate"))
        component_total = exact_total or estimated_total
        if component_total:
            component_totals[key] = component_total

        selected_total, selected_is_estimate = _selected_download_size(status)
        if selected_total:
            expected_total = selected_total
            expected_total_is_estimate = selected_is_estimate
        elif component_totals:
            expected_total = sum(component_totals.values())
            expected_total_is_estimate = exact_total is None

        if state == "downloading":
            if not download_announced:
                download_announced = True
                notify(f"Downloading {platform} media")

            done = _positive_int(status.get("downloaded_bytes")) or 0
            component_downloaded[key] = done
            aggregate_done = sum(component_downloaded.values())
            fraction = (
                min(1.0, aggregate_done / expected_total)
                if expected_total and expected_total > 0
                else None
            )
            if on_progress and fraction is not None:
                on_progress(fraction)
            if on_download_progress:
                on_download_progress(
                    DownloadProgress(
                        progress=fraction,
                        downloaded_bytes=aggregate_done,
                        total_bytes=expected_total,
                        speed_bytes_s=_positive_float(status.get("speed")),
                        total_is_estimate=expected_total_is_estimate,
                    )
                )
        elif state == "finished":
            if component_total:
                component_downloaded[key] = component_total
            notify("Download finished; merging media streams")

    selected_format = (
        BROWSER_YTDLP_FORMAT
        if settings.ytdlp_format == LEGACY_YTDLP_FORMAT
        else settings.ytdlp_format
    )
    options: dict = {
        "format": selected_format,
        "outtmpl": str(target_dir / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "retries": 3,
        "fragment_retries": 3,
    }
    if settings.cookies_from_browser:
        # yt-dlp expects a tuple; only the browser name is required.
        options["cookiesfrombrowser"] = (settings.cookies_from_browser,)
    if is_youtube_url(url) and settings.prefer_youtube_captions:
        options["writeautomaticsub"] = True
        options["subtitleslangs"] = ["en.*"]
        options["subtitlesformat"] = "json3"

    notify(f"Connecting to {platform} and selecting media streams")
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            metadata = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise _translate_ytdlp_error(exc, settings, platform=_platform_name(url)) from exc
    except Exception as exc:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise IngestError(f"Could not download {url}: {exc}") from exc

    notify("Locating downloaded media")
    downloaded = _find_downloaded_file(target_dir)
    if downloaded is None:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise IngestError("yt-dlp reported success but produced no media file.")

    notify("Validating media with ffprobe")
    info = _probe_and_validate(downloaded)
    if downloaded.suffix.lower() == ".mp4":
        notify("Optimizing MP4 for browser preview")
        optimise_mp4_for_browser(downloaded)
        info = _probe_and_validate(downloaded)
    notify("Media download is ready")

    return Source(
        id=source_id,
        # "youtube" is the legacy database value for yt-dlp-backed remote
        # sources. Keeping it avoids a destructive SQLite table rebuild while
        # the API/UI expose the actual URL and platform-specific behaviour.
        type="youtube",
        url=url,
        path=str(downloaded),
        filename=downloaded.name,
        title=(metadata or {}).get("title") or downloaded.stem,
        channel=(metadata or {}).get("uploader") or (metadata or {}).get("channel"),
        duration_s=info.duration_s or float((metadata or {}).get("duration") or 0.0),
        width=info.width,
        height=info.height,
        fps=info.fps,
        has_audio=info.has_audio,
        has_video=info.has_video,
    )


def ingest_youtube(
    url: str,
    settings: IngestSettings | None = None,
    *,
    on_progress: Callable[[float], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    on_download_progress: Callable[[DownloadProgress], None] | None = None,
) -> Source:
    """Backward-compatible YouTube-only wrapper used by older callers."""
    if not is_youtube_url(url):
        raise IngestError("That is not a YouTube URL.")
    return ingest_url(
        url,
        settings,
        on_progress=on_progress,
        on_status=on_status,
        on_download_progress=on_download_progress,
    )


def optimise_mp4_for_browser(path: Path) -> None:
    """Move MP4 metadata to the front without re-encoding.

    Browsers seek long source files through HTTP byte ranges. A freshly merged
    yt-dlp MP4 can leave its moov atom at the end, which desktop players handle
    but browser <video> elements may sit buffering on when immediately seeking
    to a clip far into the source.
    """
    path = Path(path)
    if path.suffix.lower() != ".mp4" or not path.is_file():
        return

    temp = path.with_name(f"{path.stem}.faststart.tmp.mp4")
    temp.unlink(missing_ok=True)
    try:
        ffmpeg.run(
            [
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(temp),
            ]
        )
        temp.replace(path)
        path.with_suffix(path.suffix + ".browser-ready").write_text(
            "faststart\n",
            encoding="utf-8",
        )
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def browser_preview_needs_proxy(path: Path) -> bool:
    """Return True when Chromium-style browsers need a compatibility transcode."""
    info = ffmpeg.probe(path)
    if not info.has_video:
        return False
    video_ok = (info.video_codec or "").lower() == "h264"
    audio_ok = not info.has_audio or (info.audio_codec or "").lower() == "aac"
    return not (path.suffix.lower() == ".mp4" and video_ok and audio_ok)


def build_browser_preview(source: Path, destination: Path) -> Path:
    """Create a cached H.264/AAC proxy for browser preview only."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f"{destination.stem}.tmp.mp4")
    temp.unlink(missing_ok=True)
    try:
        ffmpeg.run(
            [
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-vf",
                (
                    "scale=960:540:force_original_aspect_ratio=decrease:"
                    "force_divisible_by=2"
                ),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "fastdecode",
                "-crf",
                "30",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                str(temp),
            ]
        )
        temp.replace(destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return destination


def _positive_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _selected_download_size(status: dict) -> tuple[int | None, bool]:
    """Return the combined selected-stream size when yt-dlp exposes it."""
    info = status.get("info_dict") or {}
    requested = info.get("requested_downloads") or info.get("requested_formats")
    if not isinstance(requested, list) or not requested:
        return None, False

    total = 0
    estimated = False
    for item in requested:
        if not isinstance(item, dict):
            return None, False
        exact = _positive_int(item.get("filesize"))
        approximate = _positive_int(item.get("filesize_approx"))
        size = exact or approximate
        if size is None:
            return None, False
        total += size
        estimated = estimated or exact is None

    return (total if total > 0 else None), estimated


def _translate_ytdlp_error(
    exc: Exception, settings: IngestSettings, *, platform: str = "YouTube"
) -> IngestError:
    """Turn a yt-dlp failure into something the user can act on."""
    message = str(exc).lower()

    if "could not copy chrome cookie database" in message:
        return IngestError(
            "Chrome's cookie database is locked.",
            hint=(
                "Fully close Chrome (including background processes) and try again, "
                "or choose Firefox/Edge under Settings → Ingest → cookies from browser."
            ),
        )

    if platform == "YouTube" and any(marker in message for marker in _BOT_CHECK_MARKERS):
        if settings.cookies_from_browser:
            hint = (
                f"Cookies are already being read from {settings.cookies_from_browser}, but "
                "YouTube still refused. Make sure you are signed in to YouTube in that "
                "browser and that the browser is fully closed — it locks its cookie "
                "database while running."
            )
        else:
            hint = (
                "YouTube is asking for proof you're not a bot. Set a browser to pull "
                "cookies from — in Settings, or via config.json's "
                '`ingest.cookies_from_browser` (e.g. "chrome", "firefox", "edge"). '
                "You must be signed in to YouTube in that browser, and it must be closed "
                "while AutoClip downloads."
            )
        return IngestError("YouTube blocked this download with a bot check.", hint=hint)

    if platform == "Twitch" and any(
        marker in message
        for marker in ("subscriber", "sub-only", "login required", "authentication required")
    ):
        browser = settings.cookies_from_browser
        if browser:
            hint = (
                f"AutoClip is already reading cookies from {browser}. Make sure that browser "
                "is signed into the Twitch account that can view the VOD, then fully close it "
                "before retrying."
            )
        else:
            hint = (
                "If your account can view this VOD, set Settings → Ingest → cookies from "
                "browser to a browser where you are signed into Twitch, close that browser, "
                "and retry."
            )
        return IngestError("Twitch requires authentication for this VOD.", hint=hint)

    if "private video" in message or "members-only" in message:
        return IngestError(
            "This video is private or members-only.",
            hint="AutoClip only downloads content you can access and have the rights to use.",
        )
    if "unavailable" in message or "removed" in message:
        return IngestError("This video is unavailable or has been removed.")
    if "drm" in message:
        return IngestError(
            "This content is DRM-protected.",
            hint="AutoClip does not and will not circumvent DRM.",
        )

    return IngestError(
        f"yt-dlp could not download this {platform} video.",
        hint=(
            "Supported sites change frequently and yt-dlp is updated often. Try "
            "`autoclip update-ytdlp` to pull the latest version.\n\n"
            f"Original error: {exc}"
        ),
    )


def _find_downloaded_file(directory: Path) -> Path | None:
    """Return the largest media file in a directory, ignoring sidecars."""
    candidates = [
        p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in ACCEPTED_SUFFIXES
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------


def ingest_file(path: Path, *, move: bool = False, title: str | None = None) -> Source:
    """Register a local file as a source, copying it into AutoClip's media store.

    Copying rather than referencing in place means a job stays reproducible even
    if the user moves or deletes the original.

    Preconditions:
        path exists and is a readable media file.
    """
    path = Path(path)
    if not path.exists():
        raise IngestError(f"{path} does not exist.")
    if not path.is_file():
        raise IngestError(f"{path} is not a file.")

    suffix = path.suffix.lower()
    if suffix not in ACCEPTED_SUFFIXES:
        accepted = ", ".join(sorted(ACCEPTED_SUFFIXES))
        raise IngestError(
            f"{suffix or 'This file'} is not a supported format.",
            hint=f"Accepted formats: {accepted}",
        )

    source_id = new_id()
    target_dir = paths.source_media_dir(source_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"source{suffix}"

    try:
        if move:
            shutil.move(str(path), target)
        else:
            shutil.copy2(path, target)
    except OSError as exc:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise IngestError(f"Could not store {path.name}: {exc}") from exc

    try:
        info = _probe_and_validate(target)
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise

    return Source(
        id=source_id,
        type="upload",
        path=str(target),
        filename=path.name,
        title=title or info.title or path.stem,
        duration_s=info.duration_s,
        width=info.width,
        height=info.height,
        fps=info.fps,
        has_audio=info.has_audio,
        has_video=info.has_video,
    )


def _probe_and_validate(path: Path) -> ffmpeg.MediaInfo:
    """Probe a media file and reject anything the pipeline can't process."""
    try:
        info = ffmpeg.probe(path)
    except ffmpeg.FFmpegError as exc:
        raise IngestError(f"{path.name} could not be read as media.", hint=str(exc)) from exc

    if not info.has_audio:
        raise IngestError(
            f"{path.name} has no audio track.",
            hint=(
                "AutoClip finds clips by transcribing speech, so a file with no audio "
                "has nothing to work from."
            ),
        )
    if info.duration_s <= 0:
        raise IngestError(
            f"{path.name} reports zero duration.",
            hint="The file may be corrupt or still being written.",
        )

    return info
