"""ffmpeg and ffprobe wrappers.

Two things here are easy to get wrong and expensive to debug, so they're
centralised:

**Filtergraph path escaping.** ``ass=filename=C:\\x\\y.ass`` is not valid — the
drive-letter colon is read as an option separator, and backslashes as escapes.
Every path that reaches a filtergraph must go through
:func:`escape_filter_path`. This is the single most common Windows-only failure
in ffmpeg-based tools.

**Progress reporting.** ``-progress pipe:1`` emits machine-readable key=value
lines; parsing those beats scraping the human-readable stderr banner, which
changes between ffmpeg versions.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

ProgressCallback = Callable[[float], None]

_active_processes: set[subprocess.Popen[str]] = set()
_active_processes_lock = threading.Lock()


def _register_process(proc: subprocess.Popen[str]) -> None:
    with _active_processes_lock:
        _active_processes.add(proc)


def _unregister_process(proc: subprocess.Popen[str]) -> None:
    with _active_processes_lock:
        _active_processes.discard(proc)


def active_process_count() -> int:
    with _active_processes_lock:
        return sum(1 for proc in _active_processes if proc.poll() is None)


def terminate_all(*, grace_s: float = 1.5) -> int:
    """Terminate every ffmpeg process started through :func:`run`.

    Used during application shutdown so a render or browser-preview transcode
    cannot outlive the Python server that launched it.
    """
    with _active_processes_lock:
        processes = [proc for proc in _active_processes if proc.poll() is None]

    for proc in processes:
        try:
            proc.terminate()
        except OSError:
            pass

    deadline = time.monotonic() + max(0.0, grace_s)
    for proc in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass

    for proc in processes:
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                pass
    return len(processes)


class FFmpegError(RuntimeError):
    """An ffmpeg or ffprobe invocation failed."""

    def __init__(self, message: str, *, command: Sequence[str], stderr: str = "") -> None:
        super().__init__(message)
        self.command = list(command)
        self.stderr = stderr

    def __str__(self) -> str:
        base = super().__str__()
        tail = self.stderr.strip().splitlines()[-12:]
        if tail:
            return base + "\n" + "\n".join(tail)
        return base


@dataclass
class StreamInfo:
    codec: str
    #: Audio only.
    sample_rate: int | None = None
    channels: int | None = None


@dataclass
class MediaInfo:
    path: Path
    duration_s: float
    has_video: bool
    has_audio: bool
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    title: str | None = None

    @property
    def aspect_ratio(self) -> float | None:
        if not self.width or not self.height:
            return None
        return self.width / self.height

    @property
    def is_vertical(self) -> bool:
        ar = self.aspect_ratio
        return ar is not None and ar < 1.0


def ffmpeg_path() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise FFmpegError(
            "ffmpeg was not found on PATH. Run `autoclip doctor` for install instructions.",
            command=["ffmpeg"],
        )
    return exe


def ffprobe_path() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise FFmpegError(
            "ffprobe was not found on PATH. Run `autoclip doctor` for install instructions.",
            command=["ffprobe"],
        )
    return exe


# --------------------------------------------------------------------------
# Filtergraph escaping
# --------------------------------------------------------------------------

#: Characters that terminate or restructure a filtergraph if left unescaped.
_FILTER_SPECIALS = "\\'[],;:"


def escape_filter_path(path: Path | str) -> str:
    r"""Escape a filesystem path for use inside an ffmpeg filtergraph option.

    Filter option values pass through *two* unescaping rounds — once when the
    filtergraph is split into filters and options, once when a filter parses its
    own arguments — so each special character needs two backslashes. A single
    backslash produces the classic ``No option name near '/Users/...'`` error,
    because the drive-letter colon survives the first round and is then read as
    an option separator.

    Prefer :func:`relative_filter_workspace` for paths the user controls.
    Quoting and escaping interact badly around apostrophes, and a relative name
    has no special characters to escape in the first place.

    Preconditions:
        path is a filesystem path, not an already-escaped filtergraph fragment.
    """
    text = str(path).replace("\\", "/")
    out: list[str] = []
    for char in text:
        if char in _FILTER_SPECIALS:
            out.append("\\\\" + char)
        else:
            out.append(char)
    return "".join(out)


def escape_filter_value(value: str) -> str:
    """Escape a non-path filter option value (font names, style overrides)."""
    out: list[str] = []
    for char in value:
        if char in _FILTER_SPECIALS:
            out.append("\\" + char)
        else:
            out.append(char)
    return "".join(out)


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------


def probe(path: Path) -> MediaInfo:
    """Inspect a media file with ffprobe.

    Raises :class:`FFmpegError` if the file is unreadable or not media.
    """
    command = [
        ffprobe_path(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False, encoding="utf-8")
    if proc.returncode != 0:
        raise FFmpegError(f"Could not read {path.name}.", command=command, stderr=proc.stderr or "")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FFmpegError(
            f"ffprobe returned unreadable output for {path.name}.", command=command
        ) from exc

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    # Cover art in an audio file shows up as a video stream; a still image is
    # not something we can reframe, so don't count it as video.
    if video is not None and video.get("disposition", {}).get("attached_pic"):
        video = None

    info = MediaInfo(
        path=path,
        duration_s=float(fmt.get("duration") or 0.0),
        has_video=video is not None,
        has_audio=audio is not None,
        title=(fmt.get("tags") or {}).get("title"),
    )

    if video is not None:
        info.width = _as_int(video.get("width"))
        info.height = _as_int(video.get("height"))
        info.fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        info.video_codec = video.get("codec_name")
    if audio is not None:
        info.audio_codec = audio.get("codec_name")

    return info


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_fps(rate: str | None) -> float | None:
    """Convert ffprobe's ``"30000/1001"`` rational frame rate to a float."""
    if not rate:
        return None
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            denominator = float(den)
            return float(num) / denominator if denominator else None
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return None


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def relative_filter_workspace(subtitle_path: Path, fonts_dir: Path) -> tuple[Path, str, str]:
    """Stage subtitles and fonts so a filtergraph can reference them by bare name.

    Returns ``(working_directory, subtitle_name, fonts_name)``. Running ffmpeg
    from that directory lets the ``ass`` filter take ``filename=captions.ass``
    and ``fontsdir=fonts`` — names with no colons, separators, quotes, or
    spaces, which removes the whole escaping problem rather than trying to
    out-escape it.

    Paths passed as ordinary argv arguments (inputs, outputs) never need any of
    this; only filtergraph option values do.

    Preconditions:
        subtitle_path exists; fonts_dir exists and holds the bundled fonts.
    """
    import shutil

    workspace = subtitle_path.parent
    staged_subtitles = workspace / "captions.ass"
    if subtitle_path.resolve() != staged_subtitles.resolve():
        shutil.copyfile(subtitle_path, staged_subtitles)

    staged_fonts = workspace / "fonts"
    if fonts_dir.exists():
        staged_fonts.mkdir(exist_ok=True)
        for font in fonts_dir.iterdir():
            if font.is_file() and font.suffix.lower() in (".ttf", ".otf", ".ttc"):
                target = staged_fonts / font.name
                if not target.exists():
                    shutil.copyfile(font, target)

    return workspace, staged_subtitles.name, staged_fonts.name


def run(
    args: Sequence[str],
    *,
    total_duration_s: float | None = None,
    on_progress: ProgressCallback | None = None,
    cancelled: Callable[[], bool] | None = None,
    cwd: Path | None = None,
) -> None:
    """Run ffmpeg, optionally reporting progress as a 0..1 fraction.

    ``args`` excludes the executable name and the flags this function supplies
    (``-hide_banner``, ``-y``, ``-nostdin``, and the progress plumbing).

    Preconditions:
        total_duration_s is the expected output duration when on_progress is set;
        without it progress cannot be computed and the callback is never invoked.
    """
    command = [
        ffmpeg_path(),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
    ]
    if on_progress and total_duration_s:
        command += ["-progress", "pipe:1", "-nostats"]
    command += list(args)

    log.debug("ffmpeg %s", " ".join(command[1:]))

    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
    )
    _register_process(proc)

    try:
        if on_progress and total_duration_s and proc.stdout is not None:
            _pump_progress(proc, total_duration_s, on_progress, cancelled)
        elif cancelled is not None:
            _wait_cancellable(proc, cancelled)

        stdout, stderr = proc.communicate()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        _unregister_process(proc)

    if proc.returncode != 0:
        if cancelled is not None and cancelled():
            raise Cancelled("Render cancelled.")
        raise FFmpegError(
            f"ffmpeg exited with code {proc.returncode}.",
            command=command,
            stderr=stderr or "",
        )

    if on_progress:
        on_progress(1.0)


def run_capture(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """Run ffmpeg and capture output while keeping it in the shutdown registry."""
    command = [
        ffmpeg_path(),
        "-hide_banner",
        "-nostdin",
        *args,
    ]
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
    )
    _register_process(proc)
    try:
        stdout, stderr = proc.communicate()
        return proc.returncode, stdout or "", stderr or ""
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        _unregister_process(proc)


class Cancelled(RuntimeError):
    """Raised when a cancellation predicate returns True mid-render."""


def _pump_progress(
    proc: subprocess.Popen[str],
    total_duration_s: float,
    on_progress: ProgressCallback,
    cancelled: Callable[[], bool] | None,
) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        if cancelled is not None and cancelled():
            proc.kill()
            raise Cancelled("Render cancelled.")

        key, _, value = line.strip().partition("=")
        # out_time_us is microseconds; older builds emit out_time_ms which is
        # *also* microseconds despite the name. Handle both identically.
        if key in ("out_time_us", "out_time_ms"):
            try:
                elapsed = int(value) / 1_000_000
            except ValueError:
                continue
            on_progress(max(0.0, min(1.0, elapsed / total_duration_s)))
        elif key == "progress" and value == "end":
            on_progress(1.0)


def _wait_cancellable(proc: subprocess.Popen[str], cancelled: Callable[[], bool]) -> None:
    while True:
        try:
            proc.wait(timeout=0.5)
            return
        except subprocess.TimeoutExpired:
            if cancelled():
                proc.kill()
                raise Cancelled("Render cancelled.") from None
