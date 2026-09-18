"""Optional long-form podcast edit built from a completed AutoClip job.

The podcast continuation reuses the transcript and measured silence map that
already exist after clipping. The LLM marks only clearly removable spoken
sections; deterministic audio analysis shortens long silent gaps. The original
source audio is then rendered to a single MP3 with those ranges removed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .. import paths
from ..config import ExportSettings
from ..providers import DetectionConfig, LLMProvider, PodcastCutCandidate
from . import ffmpeg
from .highlights import HOSTED_CONCURRENCY, LOCAL_CONCURRENCY, build_windows
from .prepare import Silence
from .transcript import Transcript

log = logging.getLogger(__name__)

DEFAULT_SILENCE_THRESHOLD_S = 1.5
DEFAULT_SILENCE_KEEP_S = 0.25
MIN_AI_CUT_S = 4.0
BOUNDARY_SNAP_S = 1.5
MAX_REMOVED_FRACTION = 0.75
PODCAST_BITRATE = "192k"
PODCAST_SAMPLE_RATE = 48_000


class PodcastError(RuntimeError):
    """Podcast analysis or rendering could not complete safely."""


@dataclass(frozen=True)
class PodcastOptions:
    remove_silences: bool = True
    remove_boring_sections: bool = True
    silence_threshold_s: float = DEFAULT_SILENCE_THRESHOLD_S
    silence_keep_s: float = DEFAULT_SILENCE_KEEP_S


@dataclass(frozen=True)
class PodcastCut:
    start_s: float
    end_s: float
    kind: str
    reason: str = ""

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


@dataclass(frozen=True)
class PodcastResult:
    job_id: str
    filename: str
    size_bytes: int
    source_duration_s: float
    output_duration_s: float
    removed_duration_s: float
    silence_cut_count: int
    boring_cut_count: int
    total_cut_count: int
    provider: str
    model: str
    created_at: str
    cuts: tuple[PodcastCut, ...]

    @property
    def download_url(self) -> str:
        return f"/api/jobs/{self.job_id}/podcast/download"

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "source_duration_s": self.source_duration_s,
            "output_duration_s": self.output_duration_s,
            "removed_duration_s": self.removed_duration_s,
            "silence_cut_count": self.silence_cut_count,
            "boring_cut_count": self.boring_cut_count,
            "total_cut_count": self.total_cut_count,
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at,
            "cuts": [asdict(cut) for cut in self.cuts],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PodcastResult":
        return cls(
            job_id=str(data["job_id"]),
            filename=str(data["filename"]),
            size_bytes=int(data["size_bytes"]),
            source_duration_s=float(data["source_duration_s"]),
            output_duration_s=float(data["output_duration_s"]),
            removed_duration_s=float(data["removed_duration_s"]),
            silence_cut_count=int(data["silence_cut_count"]),
            boring_cut_count=int(data["boring_cut_count"]),
            total_cut_count=int(data["total_cut_count"]),
            provider=str(data.get("provider") or ""),
            model=str(data.get("model") or ""),
            created_at=str(data.get("created_at") or ""),
            cuts=tuple(PodcastCut(**item) for item in data.get("cuts", [])),
        )


def manifest_path(job_id: str) -> Path:
    return paths.job_work_dir(job_id) / "podcast.json"


def output_path(job_id: str) -> Path:
    return paths.exports_dir() / job_id / "podcast.mp3"


def load_result(job_id: str) -> PodcastResult | None:
    manifest = manifest_path(job_id)
    output = output_path(job_id)
    if not manifest.is_file() or not output.is_file() or output.stat().st_size <= 0:
        return None

    try:
        result = PodcastResult.from_dict(json.loads(manifest.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None

    # Paths are derived from the current storage root, so a storage move does
    # not leave a stale absolute podcast path in the manifest.
    return PodcastResult(
        **{
            **result.__dict__,
            "filename": output.name,
            "size_bytes": output.stat().st_size,
        }
    )


async def build_podcast(
    *,
    job_id: str,
    source: Path,
    source_duration_s: float,
    transcript: Transcript,
    silences: list[Silence],
    provider: LLMProvider | None,
    config: DetectionConfig,
    export_settings: ExportSettings,
    options: PodcastOptions,
    on_status: Callable[[str], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> PodcastResult:
    """Analyze and render the audio-only podcast continuation."""
    if not transcript.words:
        raise PodcastError("The transcript is empty, so there is nothing to turn into a podcast.")
    if source_duration_s <= 0:
        source_duration_s = transcript.duration
    if source_duration_s <= 0:
        raise PodcastError("The source duration is unavailable.")

    def status(message: str) -> None:
        log.info("Podcast %s: %s", job_id, message)
        if on_status:
            on_status(message)

    status("Preparing podcast edit")
    if on_progress:
        on_progress(0.02)

    boring_cuts: list[PodcastCut] = []
    if options.remove_boring_sections:
        if provider is None:
            raise PodcastError("An AI provider is required to remove boring spoken sections.")
        status(f"Analyzing transcript with {provider.name}")
        candidates = await analyze_boring_sections(
            transcript,
            provider,
            config,
            on_progress=(
                (lambda fraction: on_progress(0.05 + fraction * 0.60))
                if on_progress
                else None
            ),
        )
        boring_cuts = boring_sections_to_cuts(transcript, candidates, silences)
    elif on_progress:
        on_progress(0.65)

    silence_cuts: list[PodcastCut] = []
    if options.remove_silences:
        status("Shortening long silent gaps")
        silence_cuts = silence_ranges_to_cuts(
            silences,
            source_duration_s=source_duration_s,
            threshold_s=options.silence_threshold_s,
            keep_s=options.silence_keep_s,
        )

    cuts = merge_cuts([*boring_cuts, *silence_cuts], source_duration_s)
    removed = sum(cut.duration_s for cut in cuts)
    if removed / source_duration_s > MAX_REMOVED_FRACTION:
        raise PodcastError(
            "The proposed podcast edit would remove more than 75% of the source. "
            "AutoClip stopped instead of producing an over-edited file."
        )

    output_duration = max(0.0, source_duration_s - removed)
    if output_duration < 1.0:
        raise PodcastError("The proposed podcast edit leaves no usable audio.")

    status(
        f"Rendering podcast — removing {removed / 60:.1f} minutes "
        f"across {len(cuts)} cuts"
    )
    destination = output_path(job_id)
    work_dir = paths.job_work_dir(job_id)
    await asyncio.to_thread(
        render_podcast,
        source,
        destination,
        cuts,
        source_duration_s=source_duration_s,
        output_duration_s=output_duration,
        loudness_lufs=export_settings.loudness_lufs,
        work_dir=work_dir,
        on_progress=(
            (lambda fraction: on_progress(0.68 + fraction * 0.32))
            if on_progress
            else None
        ),
    )

    result = PodcastResult(
        job_id=job_id,
        filename=destination.name,
        size_bytes=destination.stat().st_size,
        source_duration_s=source_duration_s,
        output_duration_s=output_duration,
        removed_duration_s=removed,
        silence_cut_count=sum(cut.kind in ("silence", "mixed") for cut in cuts),
        boring_cut_count=sum(cut.kind in ("boring", "mixed") for cut in cuts),
        total_cut_count=len(cuts),
        provider=provider.name if options.remove_boring_sections and provider else "",
        model=provider.model if options.remove_boring_sections and provider else "",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        cuts=tuple(cuts),
    )
    manifest = manifest_path(job_id)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")

    status("Podcast ready")
    if on_progress:
        on_progress(1.0)
    return result


async def analyze_boring_sections(
    transcript: Transcript,
    provider: LLMProvider,
    config: DetectionConfig,
    *,
    on_progress: Callable[[float], None] | None = None,
) -> list[PodcastCutCandidate]:
    """Ask the selected provider to identify conservative spoken cuts."""
    windows = build_windows(transcript)
    if not windows:
        return []

    concurrency = LOCAL_CONCURRENCY if provider.name == "ollama" else HOSTED_CONCURRENCY
    semaphore = asyncio.Semaphore(concurrency)
    completed = 0
    failures: list[str] = []
    lock = asyncio.Lock()

    async def run_window(window):
        nonlocal completed
        async with semaphore:
            try:
                result = await provider.detect_podcast_cuts(window, config)
                cuts = result.cuts
            except Exception as exc:
                log.warning(
                    "Podcast analysis window %d-%d failed: %s",
                    window.first_word,
                    window.last_word,
                    exc,
                )
                failures.append(f"{window.first_word}-{window.last_word}: {exc}")
                cuts = []
            async with lock:
                completed += 1
                if on_progress:
                    on_progress(completed / len(windows))
            return cuts

    results = await asyncio.gather(*(run_window(window) for window in windows))
    if failures:
        raise PodcastError(
            f"Podcast transcript analysis failed for {len(failures)} of {len(windows)} "
            "windows. No audio was changed. Try the job again or use a different model."
        )

    return dedupe_candidates([candidate for group in results for candidate in group])


def dedupe_candidates(
    candidates: list[PodcastCutCandidate],
    *,
    iou_threshold: float = 0.5,
) -> list[PodcastCutCandidate]:
    """Deduplicate cuts proposed in overlapping transcript windows."""
    ordered = sorted(
        candidates,
        key=lambda item: item.end_word_index - item.start_word_index,
        reverse=True,
    )
    kept: list[PodcastCutCandidate] = []
    for candidate in ordered:
        if any(
            _index_iou(
                candidate.start_word_index,
                candidate.end_word_index,
                existing.start_word_index,
                existing.end_word_index,
            )
            >= iou_threshold
            for existing in kept
        ):
            continue
        kept.append(candidate)
    return sorted(kept, key=lambda item: item.start_word_index)


def boring_sections_to_cuts(
    transcript: Transcript,
    candidates: list[PodcastCutCandidate],
    silences: list[Silence],
) -> list[PodcastCut]:
    cuts: list[PodcastCut] = []
    for candidate in candidates:
        start_s, end_s = transcript.time_range(
            candidate.start_word_index,
            candidate.end_word_index,
        )
        if end_s - start_s < MIN_AI_CUT_S:
            continue

        start_s = _snap_boundary(start_s, silences)
        end_s = _snap_boundary(end_s, silences)
        if end_s - start_s < MIN_AI_CUT_S:
            continue

        cuts.append(
            PodcastCut(
                start_s=start_s,
                end_s=end_s,
                kind="boring",
                reason=candidate.reason,
            )
        )
    return cuts


def silence_ranges_to_cuts(
    silences: list[Silence],
    *,
    source_duration_s: float,
    threshold_s: float = DEFAULT_SILENCE_THRESHOLD_S,
    keep_s: float = DEFAULT_SILENCE_KEEP_S,
) -> list[PodcastCut]:
    """Shorten long silences while preserving a natural pause around each edit."""
    cuts: list[PodcastCut] = []
    threshold_s = max(0.5, threshold_s)
    keep_s = max(0.0, keep_s)

    for silence in silences:
        start = max(0.0, silence.start)
        end = min(source_duration_s, silence.end)
        if end - start < threshold_s:
            continue

        cut_start = start + keep_s
        cut_end = end - keep_s
        if cut_end - cut_start < 0.1:
            continue

        cuts.append(
            PodcastCut(
                start_s=cut_start,
                end_s=cut_end,
                kind="silence",
                reason="Long silent gap",
            )
        )
    return cuts


def merge_cuts(cuts: list[PodcastCut], source_duration_s: float) -> list[PodcastCut]:
    """Clamp, sort, and merge overlapping cuts into a render-safe plan."""
    normalized = [
        PodcastCut(
            start_s=max(0.0, min(source_duration_s, cut.start_s)),
            end_s=max(0.0, min(source_duration_s, cut.end_s)),
            kind=cut.kind,
            reason=cut.reason,
        )
        for cut in cuts
        if cut.end_s - cut.start_s > 0.01
    ]
    normalized.sort(key=lambda cut: (cut.start_s, cut.end_s))

    merged: list[PodcastCut] = []
    for cut in normalized:
        if not merged or cut.start_s > merged[-1].end_s + 0.05:
            merged.append(cut)
            continue

        previous = merged[-1]
        kind = previous.kind if previous.kind == cut.kind else "mixed"
        reasons = [reason for reason in (previous.reason, cut.reason) if reason]
        reason = "; ".join(dict.fromkeys(reasons))
        merged[-1] = PodcastCut(
            start_s=previous.start_s,
            end_s=max(previous.end_s, cut.end_s),
            kind=kind,
            reason=reason,
        )
    return merged


def render_podcast(
    source: Path,
    destination: Path,
    cuts: list[PodcastCut],
    *,
    source_duration_s: float,
    output_duration_s: float,
    loudness_lufs: float,
    work_dir: Path,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Render the original source audio with the podcast cut plan applied."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    loudnorm = f"loudnorm=I={loudness_lufs}:TP=-1.5:LRA=11"
    if cuts:
        expression = _keep_expression(cuts)
        filtergraph = (
            f"[0:a]aselect='{expression}',asetpts=N/SR/TB,{loudnorm}[aout]"
        )
    else:
        filtergraph = f"[0:a]{loudnorm}[aout]"

    script = work_dir / "podcast-filter.txt"
    script.write_text(filtergraph, encoding="utf-8")

    args = [
        "-i",
        str(source),
        "-filter_complex_script",
        str(script),
        "-map",
        "[aout]",
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        PODCAST_BITRATE,
        "-ar",
        str(PODCAST_SAMPLE_RATE),
        "-metadata",
        "title=AutoClip podcast edit",
        str(destination),
    ]

    try:
        ffmpeg.run(
            args,
            total_duration_s=output_duration_s,
            on_progress=on_progress,
        )
    except ffmpeg.FFmpegError as exc:
        raise PodcastError(f"Could not render the podcast audio.\n{exc}") from exc

    if not destination.is_file() or destination.stat().st_size <= 0:
        raise PodcastError("ffmpeg reported success but the podcast file is empty.")
    return destination


def _keep_expression(cuts: list[PodcastCut]) -> str:
    removed = "+".join(
        f"between(t\\,{cut.start_s:.4f}\\,{cut.end_s:.4f})" for cut in cuts
    )
    return f"not({removed})"


def _snap_boundary(value: float, silences: list[Silence]) -> float:
    candidates = [
        silence.midpoint
        for silence in silences
        if abs(silence.midpoint - value) <= BOUNDARY_SNAP_S
    ]
    return min(candidates, key=lambda point: abs(point - value)) if candidates else value


def _index_iou(a_start: int, a_end: int, b_start: int, b_end: int) -> float:
    intersection = max(0, min(a_end, b_end) - max(a_start, b_start) + 1)
    if intersection == 0:
        return 0.0
    union = (a_end - a_start + 1) + (b_end - b_start + 1) - intersection
    return intersection / union if union else 0.0
