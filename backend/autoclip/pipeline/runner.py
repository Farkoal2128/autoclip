"""Pipeline orchestration.

Runs a job stage by stage, writing each stage's artifacts into
``work/{job_id}/``. Because every stage's output is a file on disk, a retry
skips everything already done and resumes at the stage that failed — which
matters when stage two took eleven minutes and stage four hit a rate limit.

Progress is reported through a callback rather than written directly, so the
same runner serves the CLI (a progress bar) and the web API (an SSE stream).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import paths, reuse, storage
from ..config import Settings
from ..config import load as load_settings
from ..db import store
from ..db.models import Clip, Export, Job, Source, new_id, utcnow
from ..db.models import Transcript as TranscriptRow
from ..providers import build_provider, detection_config
from . import Stage, captions, export, ffmpeg, highlights, prepare, transcribe
from .prepare import Silence
from .reframe.croppath import CropPath, centre_crop
from .transcript import Transcript

log = logging.getLogger(__name__)

#: Weight of each stage in the overall progress bar. Rough proportions of wall
#: time on a mid-range machine — transcription and export dominate.
STAGE_WEIGHTS: dict[Stage, float] = {
    Stage.PREPARE: 0.05,
    Stage.TRANSCRIBE: 0.35,
    Stage.HIGHLIGHTS: 0.15,
    Stage.REFRAME: 0.20,
    Stage.CAPTIONS: 0.02,
    Stage.EXPORT: 0.23,
}


class JobCancelled(RuntimeError):
    """The job was cancelled by the user."""


class PipelineError(RuntimeError):
    """A stage failed in a way the user should see."""

    def __init__(self, message: str, *, stage: Stage) -> None:
        super().__init__(message)
        self.stage = stage


@dataclass
class ProgressEvent:
    stage: Stage
    #: Progress within the stage, 0..1.
    stage_progress: float
    #: Progress across the whole job, 0..1.
    overall: float
    message: str = ""


ProgressHandler = Callable[[ProgressEvent], None]


class JobWorkspace:
    """Paths for one job's intermediate artifacts."""

    def __init__(self, job_id: str) -> None:
        self.root = paths.job_work_dir(job_id)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def audio(self) -> Path:
        return self.root / "audio.wav"

    @property
    def transcript(self) -> Path:
        return self.root / "transcript.json"

    @property
    def silences(self) -> Path:
        return self.root / "silences.json"

    def crop_path(self, clip_id: str) -> Path:
        return self.root / "crops" / f"{clip_id}.json"

    def twitch_chat(self, clip_id: str) -> Path:
        return self.root / "twitch-chat" / f"{clip_id}.json"

    def twitch_chat_assets(self, clip_id: str) -> Path:
        return self.root / "twitch-chat" / f"{clip_id}-assets"

    @property
    def preview_media(self) -> Path:
        return self.root / "preview.mp4"

    @property
    def captions_dir(self) -> Path:
        return self.root / "captions"


class PipelineRunner:
    """Executes one job."""

    def __init__(
        self,
        job: Job,
        source: Source,
        *,
        settings: Settings | None = None,
        on_progress: ProgressHandler | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.job = job
        self.source = source
        self.source.path = str(storage.resolve_source_path(source))
        if settings is not None:
            self.settings = settings
        elif job.settings:
            try:
                self.settings = Settings.model_validate(job.settings)
            except Exception:
                log.warning("Job %s has invalid saved settings; using current settings.", job.id)
                self.settings = load_settings()
        else:
            self.settings = load_settings()
        self.on_progress = on_progress
        self._is_cancelled = is_cancelled or (lambda: False)
        self.workspace = JobWorkspace(job.id)
        self._completed_weight = 0.0
        self._last_progress_message = ""

    # -- progress ----------------------------------------------------------

    def _check_cancelled(self) -> None:
        if self._is_cancelled():
            raise JobCancelled("Job cancelled.")

    def _emit(
        self,
        stage: Stage,
        stage_progress: float,
        message: str = "",
        *,
        overall: float | None = None,
    ) -> None:
        if overall is None:
            overall = self._completed_weight + STAGE_WEIGHTS[stage] * stage_progress
        overall = min(1.0, overall)
        resolved_message = message or stage.label
        store.update_job(self.job.id, current_stage=stage.value, progress=round(overall, 4))
        if resolved_message != self._last_progress_message:
            log.info("Job %s [%s] %s", self.job.id, stage.value, resolved_message)
            self._last_progress_message = resolved_message
        if self.on_progress:
            self.on_progress(
                ProgressEvent(
                    stage=stage,
                    stage_progress=stage_progress,
                    overall=overall,
                    message=resolved_message,
                )
            )

    def _finish_stage(self, stage: Stage, message: str | None = None) -> None:
        """Mark a stage complete.

        ``overall`` is passed explicitly rather than derived. Deriving it after
        incrementing the accumulated weight counts the stage twice, so the bar
        jumped past the true total and then snapped backwards when the next
        stage reported 0 — visibly, at every stage boundary.
        """
        self._completed_weight += STAGE_WEIGHTS[stage]
        self._emit(
            stage,
            1.0,
            message or f"{stage.label} complete",
            overall=self._completed_weight,
        )

    def _stage_progress(
        self, stage: Stage, message: str | None = None
    ) -> Callable[[float], None]:
        def report(fraction: float) -> None:
            self._emit(stage, max(0.0, min(1.0, fraction)), message or stage.label)

        return report

    # -- entry point -------------------------------------------------------

    async def run(self) -> list[Clip]:
        """Run the full pipeline and return the exported clips."""
        store.update_job(self.job.id, status="running", started_at=utcnow(), error=None)

        try:
            audio = self._stage_prepare()
            transcript = self._stage_transcribe(audio)
            silences = self._load_or_detect_silences(audio)
            clips = await self._stage_highlights(transcript, silences)
            crop_paths = self._stage_reframe(clips)
            self._stage_captions(clips, transcript)
            self._stage_export(clips, transcript, crop_paths)
        except JobCancelled:
            store.update_job(self.job.id, status="cancelled", finished_at=utcnow(), progress=0.0)
            raise
        except Exception as exc:
            log.exception("Job %s failed.", self.job.id)
            store.update_job(self.job.id, status="failed", error=str(exc), finished_at=utcnow())
            raise

        store.update_job(self.job.id, status="done", progress=1.0, finished_at=utcnow())
        return clips

    # -- stages ------------------------------------------------------------

    def _stage_prepare(self) -> Path:
        stage = Stage.PREPARE
        self._check_cancelled()
        source_path = Path(self.source.path)

        if self.workspace.audio.exists() and self.workspace.audio.stat().st_size > 0:
            self._emit(stage, 0.0, "Using cached extracted audio")
        else:
            self._emit(stage, 0.0, "Extracting audio from source media")
            prepare.extract_audio(
                source_path,
                self.workspace.audio,
                duration_s=self.source.duration_s,
                on_progress=self._stage_progress(stage, "Extracting audio from source media"),
            )

        self._finish_stage(stage, "Media preparation complete")
        return self.workspace.audio

    def _stage_transcribe(self, audio: Path) -> Transcript:
        stage = Stage.TRANSCRIBE
        self._check_cancelled()

        if self.workspace.transcript.exists():
            self._emit(stage, 0.0, "Using cached word-level transcript")
            transcript = Transcript.load(self.workspace.transcript)
            self._finish_stage(stage, "Transcript ready")
            return transcript

        transcribe_message = f"Transcribing with Whisper {self.settings.whisper.model}"
        self._emit(stage, 0.0, transcribe_message)
        transcript = transcribe.transcribe(
            audio,
            self.settings.whisper,
            duration_s=self.source.duration_s,
            on_progress=self._stage_progress(stage, transcribe_message),
            cancelled=self._is_cancelled,
        )

        if self.settings.whisper.diarization:
            from ..config import HF_TOKEN_KEY, get_secret

            self._emit(stage, 0.95, "Identifying speakers")
            transcribe.diarize(audio, transcript, hf_token=get_secret(HF_TOKEN_KEY, self.settings))

        self._emit(stage, 0.99, f"Saving {len(transcript.words):,} timed words")
        transcript.save(self.workspace.transcript)
        store.upsert_transcript(
            TranscriptRow(
                job_id=self.job.id,
                json_path=str(self.workspace.transcript),
                language=transcript.language,
                model=transcript.model,
                has_diarization=transcript.has_diarization,
                word_count=len(transcript.words),
                source=transcript.source,
            )
        )

        self._finish_stage(stage, "Transcript ready")
        return transcript

    def _load_or_detect_silences(self, audio: Path) -> list[Silence]:
        stage = Stage.HIGHLIGHTS
        if self.workspace.silences.exists():
            self._emit(stage, 0.0, "Using cached silence boundaries")
            raw = json.loads(self.workspace.silences.read_text(encoding="utf-8"))
            return [Silence(**item) for item in raw]

        self._emit(stage, 0.0, "Detecting silence boundaries")
        silences = prepare.detect_silences(audio)
        self.workspace.silences.write_text(
            json.dumps([{"start": s.start, "end": s.end} for s in silences]),
            encoding="utf-8",
        )
        return silences

    async def _stage_highlights(
        self, transcript: Transcript, silences: list[Silence]
    ) -> list[Clip]:
        stage = Stage.HIGHLIGHTS
        self._check_cancelled()

        existing = store.list_clips(self.job.id)
        if existing:
            self._emit(stage, 0.0, f"Using {len(existing)} cached highlight candidates")
            self._finish_stage(stage, "Highlights ready")
            return existing

        provider_name = self.job.provider or self.settings.active_provider
        provider = build_provider(provider_name, self.settings)
        config = detection_config(self.settings)

        rerun = reuse.rerun_meta(self.job)
        if rerun.exclude_ranges:
            config.exclude_ranges = list(rerun.exclude_ranges)

        if config.exclude_ranges:
            self._emit(
                stage,
                0.0,
                (
                    f"Finding different highlights with {provider.name} "
                    f"(avoiding {len(config.exclude_ranges)} earlier clips)"
                ),
            )
        else:
            self._emit(stage, 0.0, f"Finding highlights with {provider.name}")
        clips = await highlights.detect(
            transcript,
            provider,
            config,
            job_id=self.job.id,
            silences=silences,
            on_progress=self._stage_progress(
                stage,
                (
                    f"Searching for different moments with {provider.name}"
                    if config.exclude_ranges
                    else f"Analyzing transcript windows with {provider.name}"
                ),
            ),
        )

        self._emit(stage, 0.98, f"Selected {len(clips)} highlight candidates")
        store.replace_clips(self.job.id, clips)
        self._finish_stage(stage, "Highlights ready")
        return clips

    def _stage_reframe(self, clips: list[Clip]) -> dict[str, CropPath]:
        """Build a static center-crop path for each clip.

        Manual Layout edits are the place for subject-specific framing. Keeping
        automatic Reframe deterministic avoids a second, slower tracking system
        that competes with those user edits.
        """
        stage = Stage.REFRAME
        self._check_cancelled()
        source_path = Path(self.source.path)

        crop_paths: dict[str, CropPath] = {}

        if not self.source.has_video:
            self._emit(stage, 0.0, "Audio-only source; skipping visual reframing")
            self._finish_stage(stage, "Reframe skipped")
            return crop_paths

        ratio = self.settings.export.ratio
        aspect_w, aspect_h = (
            (9, 16) if ratio == "9:16" else (1, 1) if ratio == "1:1" else (16, 9)
        )
        output_w, output_h = export.ratio_dimensions(ratio)
        info = ffmpeg.probe(source_path)
        source_w = info.width or output_w
        source_h = info.height or output_h

        for index, clip in enumerate(clips):
            self._check_cancelled()
            self._emit(
                stage,
                index / len(clips),
                f"Reframing clip {index + 1}/{len(clips)}",
            )
            crop_path = centre_crop(
                source_w,
                source_h,
                clip.end_s - clip.start_s,
                aspect_w=aspect_w,
                aspect_h=aspect_h,
            )
            crop_path.save(self.workspace.crop_path(clip.id))
            crop_paths[clip.id] = crop_path

            self._emit(
                stage,
                (index + 1) / len(clips),
                f"Framing ready for clip {index + 1}/{len(clips)}",
            )

        self._finish_stage(stage, "Reframing complete")
        return crop_paths

    def _stage_captions(self, clips: list[Clip], transcript: Transcript) -> None:
        stage = Stage.CAPTIONS
        self._check_cancelled()
        # Caption files are written during export, where the output dimensions
        # are known. This stage validates the style so a typo fails fast rather
        # than after the reframe work is already done.
        self._emit(
            stage,
            0.0,
            f"Validating caption style {self.settings.export.caption_style}",
        )
        captions.get_style(self.settings.export.caption_style)
        self._finish_stage(stage, "Caption timing plan ready")

    def _stage_export(
        self,
        clips: list[Clip],
        transcript: Transcript,
        crop_paths: dict[str, CropPath],
    ) -> None:
        stage = Stage.EXPORT
        self._check_cancelled()

        style = captions.get_style(self.settings.export.caption_style)
        ratio = self.settings.export.ratio
        source_path = Path(self.source.path)
        destination_dir = paths.exports_dir() / self.job.id
        destination_dir.mkdir(parents=True, exist_ok=True)

        for index, clip in enumerate(clips):
            self._check_cancelled()

            crop_path = crop_paths.get(clip.id) or self._fallback_crop_path(clip, ratio)
            words = transcript.slice(clip.start_word, clip.end_word)
            destination = destination_dir / export.output_filename(
                clip.title or f"clip-{clip.rank}", ratio
            )

            request = export.ExportRequest(
                source=source_path,
                destination=destination,
                start_s=clip.start_s,
                end_s=clip.end_s,
                crop_path=crop_path,
                words=words,
                style=style,
                ratio=ratio,
            )

            def clip_progress(fraction: float, i: int = index) -> None:
                self._emit(
                    stage,
                    (i + fraction) / len(clips),
                    f"Rendering clip {i + 1}/{len(clips)}",
                )

            export.export_clip(
                request,
                work_dir=self.workspace.captions_dir,
                settings=self.settings.export,
                on_progress=clip_progress,
                cancelled=self._is_cancelled,
            )

            store.create_export(
                Export(
                    id=new_id(),
                    clip_id=clip.id,
                    path=str(destination),
                    ratio=ratio,
                    style=style.key,
                    size_bytes=destination.stat().st_size,
                )
            )
            store.update_clip(clip.id, status="exported")
            self._emit(
                stage,
                (index + 1) / len(clips),
                f"Saved clip {index + 1}/{len(clips)}",
            )

        self._finish_stage(stage, "All exports complete")

    def _fallback_crop_path(self, clip: Clip, ratio: str) -> CropPath:
        """Centre crop for sources with no reframe data (audio-only, or a failure)."""
        from .reframe.croppath import centre_crop

        width, height = export.ratio_dimensions(ratio)
        info = ffmpeg.probe(Path(self.source.path))
        return centre_crop(
            info.width or width,
            info.height or height,
            clip.end_s - clip.start_s,
            aspect_w=9 if ratio == "9:16" else (1 if ratio == "1:1" else 16),
            aspect_h=16 if ratio == "9:16" else (1 if ratio == "1:1" else 9),
        )


async def run_job(
    job_id: str,
    *,
    settings: Settings | None = None,
    on_progress: ProgressHandler | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> list[Clip]:
    """Load a job and run it to completion."""
    job = store.get_job(job_id)
    if job is None:
        raise PipelineError(f"Job {job_id} not found.", stage=Stage.PREPARE)

    source = store.get_source(job.source_id)
    if source is None:
        raise PipelineError(f"Source {job.source_id} not found.", stage=Stage.PREPARE)

    runner = PipelineRunner(
        job, source, settings=settings, on_progress=on_progress, is_cancelled=is_cancelled
    )
    # The pipeline is mostly blocking work (ffmpeg and Whisper) with one
    # async stage. Running it in a worker thread keeps the web server's event
    # loop responsive while a job is going.
    return await asyncio.get_running_loop().run_in_executor(None, _run_sync, runner)


def _run_sync(runner: PipelineRunner) -> list[Clip]:
    return asyncio.run(runner.run())
