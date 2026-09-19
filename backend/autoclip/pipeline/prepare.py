"""Prepare stage — normalise media into what later stages expect.

Produces a 16 kHz mono WAV (what Whisper wants). Silence detection runs from
that cached audio later, so the prepare stage does not decode the source video
again just to create unused preview assets.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg

log = logging.getLogger(__name__)

#: Whisper resamples to 16 kHz mono internally; doing it once up front avoids
#: repeating the work on every model invocation.
AUDIO_SAMPLE_RATE = 16_000


@dataclass
class Silence:
    start: float
    end: float

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2

    @property
    def duration(self) -> float:
        return self.end - self.start


def extract_audio(
    source: Path,
    destination: Path,
    *,
    duration_s: float | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Extract mono 16 kHz PCM WAV from any input."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(
        [
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        total_duration_s=duration_s,
        on_progress=on_progress,
    )
    return destination



_SILENCE_START = re.compile(r"silence_start:\s*([-\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([-\d.]+)")


def detect_silences(
    audio: Path,
    *,
    noise_db: float = -32.0,
    min_duration_s: float = 0.20,
) -> list[Silence]:
    """Find silent spans using ffmpeg's ``silencedetect``.

    Clip boundaries land far better inside these than at a fixed offset from a
    sentence end: a fixed pad clips breaths and plosives, while a trough is
    where a human editor would cut.

    Preconditions:
        audio is a decodable audio file; video inputs work but waste decode time.
    """
    command = [
        ffmpeg.ffmpeg_path(),
        "-hide_banner",
        "-nostdin",
        "-i",
        str(audio),
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_duration_s}",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(
        command, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        log.warning("silencedetect failed; boundary refinement will fall back to fixed padding.")
        return []

    silences: list[Silence] = []
    pending_start: float | None = None
    for line in (proc.stderr or "").splitlines():
        if match := _SILENCE_START.search(line):
            pending_start = float(match.group(1))
        elif match := _SILENCE_END.search(line):
            end = float(match.group(1))
            start = pending_start if pending_start is not None else end
            silences.append(Silence(start=max(0.0, start), end=end))
            pending_start = None

    return silences
