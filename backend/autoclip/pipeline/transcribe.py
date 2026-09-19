"""Transcribe stage — audio to word-level timestamps.

faster-whisper does the transcription; WhisperX (optional) adds speaker labels.
Word-level timing is non-negotiable: clip boundaries, caption animation, and
trim-handle snapping all derive from it.
"""

from __future__ import annotations

import logging
import multiprocessing
import tempfile
from collections.abc import Callable
from pathlib import Path

from ..config import WhisperSettings
from ..system import GPUInfo, report
from .transcript import Segment, Transcript, Word

log = logging.getLogger(__name__)


class TranscriptionError(RuntimeError):
    """Transcription could not be completed."""


class TranscriptionCancelled(RuntimeError):
    """Whisper was force-stopped because the job was cancelled."""


def resolve_compute(settings: WhisperSettings, gpu: GPUInfo | None = None) -> tuple[str, str]:
    """Return the ``(device, compute_type)`` to run Whisper with.

    An explicit ``settings.compute_type`` wins, but only if the device actually
    supports it. An override the backend will reject fails at model load with a
    message that points at cuDNN rather than at the setting, so an unsupported
    override is warned about and ignored rather than honoured into a crash.
    """
    gpu = gpu if gpu is not None else report().gpu
    automatic = gpu.compute_type

    override = settings.compute_type
    if not override:
        return gpu.device, automatic

    supported = gpu.supported_compute_types
    if supported and override not in supported:
        log.warning(
            "whisper.compute_type is set to %r, which %s does not support "
            "(supported: %s). Using %r instead.",
            override,
            gpu.device,
            ", ".join(sorted(supported)),
            automatic,
        )
        return gpu.device, automatic

    return gpu.device, override


def _model_load_error(exc: Exception, device: str, compute_type: str) -> TranscriptionError:
    """Turn a Whisper model-load failure into something actionable.

    The two causes look identical from the outside and need opposite fixes, so
    they're distinguished rather than lumped under one guess.
    """
    message = str(exc)
    lowered = message.lower()

    if "compute type" in lowered or "not support" in lowered:
        supported = report().gpu.supported_compute_types
        options = ", ".join(sorted(supported)) if supported else "unknown"
        return TranscriptionError(
            f"This device does not support the '{compute_type}' compute type.\n"
            f"Supported on {device}: {options}.\n\n"
            "Clear whisper.compute_type in config.json to let AutoClip choose, "
            "or set it to one of the supported values."
        )

    if device == "cuda" and ("cudnn" in lowered or "library" in lowered or "dll" in lowered):
        return TranscriptionError(
            f"Could not load Whisper on the GPU: {message}\n\n"
            "This is a missing CUDA runtime library. Install it with "
            "`uv pip install 'autoclip[gpu]'`, then retry — the job resumes "
            "from this stage."
        )

    return TranscriptionError(f"Could not load the Whisper model: {message}")


def _transcribe_direct(
    audio: Path,
    settings: WhisperSettings | None = None,
    *,
    duration_s: float | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Transcript:
    """Transcribe an audio file into a :class:`Transcript` with word timings.

    Preconditions:
        audio is a decodable audio file, ideally 16 kHz mono as produced by
        :func:`autoclip.pipeline.prepare.extract_audio`.
    """
    settings = settings or WhisperSettings()

    # Must happen before faster-whisper pulls in CTranslate2, which resolves its
    # CUDA dependencies at import.
    from ..cuda import ensure_cuda_libraries

    ensure_cuda_libraries()

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - faster-whisper is a core dep
        raise TranscriptionError(
            "faster-whisper is not installed. Run `autoclip doctor` for details."
        ) from exc

    device, compute_type = resolve_compute(settings)
    log.info(
        "Transcribing with model=%s device=%s compute_type=%s",
        settings.model,
        device,
        compute_type,
    )

    try:
        model = WhisperModel(settings.model, device=device, compute_type=compute_type)
    except Exception as exc:
        raise _model_load_error(exc, device, compute_type) from exc

    segments_iter, info = model.transcribe(
        str(audio),
        language=settings.language or None,
        word_timestamps=True,
        vad_filter=True,
        # Trims long silences before decoding, which both speeds things up and
        # stops Whisper hallucinating text into empty audio.
        vad_parameters={"min_silence_duration_ms": 500},
    )

    total = duration_s or getattr(info, "duration", 0.0) or 0.0
    transcript = Transcript(
        language=getattr(info, "language", "") or settings.language,
        model=settings.model,
        source="whisper",
    )

    for segment in segments_iter:
        if cancelled is not None and cancelled():
            raise TranscriptionError("Transcription cancelled.")

        first_word = len(transcript.words)
        for word in getattr(segment, "words", None) or []:
            text = (word.word or "").strip()
            if not text:
                continue
            transcript.words.append(Word(text=text, start=float(word.start), end=float(word.end)))

        # A segment with no word timings still carries text worth keeping for
        # display, but it can't contribute to word-indexed clip boundaries.
        last_word = max(first_word, len(transcript.words) - 1)
        transcript.segments.append(
            Segment(
                text=(segment.text or "").strip(),
                start=float(segment.start),
                end=float(segment.end),
                first_word=first_word,
                last_word=last_word,
            )
        )

        if on_progress and total:
            on_progress(min(1.0, float(segment.end) / total))

    if not transcript.words:
        raise TranscriptionError(
            "No speech was detected in this audio. If the file really does contain "
            "speech, try a larger Whisper model or check that the correct audio track "
            "was extracted."
        )

    if on_progress:
        on_progress(1.0)

    log.info(
        "Transcribed %d words in %d segments.", len(transcript.words), len(transcript.segments)
    )
    return transcript


def _transcribe_worker(
    audio: str,
    settings_data: dict,
    duration_s: float | None,
    output_path: str,
    updates,
) -> None:
    """Child-process entry point so CTranslate2 can be terminated immediately."""
    settings = WhisperSettings.model_validate(settings_data)

    def report_progress(fraction: float) -> None:
        updates.send(("progress", float(fraction)))

    try:
        transcript = _transcribe_direct(
            Path(audio),
            settings,
            duration_s=duration_s,
            on_progress=report_progress,
            cancelled=None,
        )
        transcript.save(Path(output_path))
    except Exception as exc:
        updates.send(("error", str(exc)))
        return

    updates.send(("done", ""))


def _terminate_process(proc: multiprocessing.Process) -> None:
    """Stop a Whisper child promptly, escalating if native code ignores terminate."""
    if not proc.is_alive():
        proc.join(timeout=0.2)
        return

    proc.terminate()
    proc.join(timeout=2.0)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=2.0)


def _transcribe_isolated(
    audio: Path,
    settings: WhisperSettings,
    *,
    duration_s: float | None,
    on_progress: Callable[[float], None] | None,
    cancelled: Callable[[], bool],
) -> Transcript:
    """Run Whisper in a killable subprocess and poll cancellation every 100 ms."""
    context = multiprocessing.get_context("spawn")
    updates, worker_updates = context.Pipe(duplex=False)
    error_message: str | None = None
    done = False

    with tempfile.TemporaryDirectory(prefix="autoclip-whisper-") as temp_dir:
        output_path = Path(temp_dir) / "transcript.json"
        proc = context.Process(
            target=_transcribe_worker,
            args=(
                str(audio),
                settings.model_dump(mode="json"),
                duration_s,
                str(output_path),
                worker_updates,
            ),
            name="autoclip-whisper",
            daemon=True,
        )
        proc.start()
        worker_updates.close()

        def drain_updates() -> None:
            nonlocal error_message, done
            while updates.poll():
                try:
                    kind, value = updates.recv()
                except (EOFError, OSError):
                    return

                if kind == "progress" and on_progress is not None:
                    on_progress(float(value))
                elif kind == "error":
                    error_message = str(value)
                elif kind == "done":
                    done = True

        try:
            while proc.is_alive():
                drain_updates()
                if cancelled():
                    _terminate_process(proc)
                    raise TranscriptionCancelled("Transcription cancelled.")
                proc.join(timeout=0.1)

            proc.join()
            drain_updates()

            # The final pipe message can trail process exit by a few milliseconds.
            if not done and error_message is None and updates.poll(0.25):
                try:
                    kind, value = updates.recv()
                except (EOFError, OSError):
                    kind = ""
                    value = ""
                if kind == "error":
                    error_message = str(value)
                elif kind == "done":
                    done = True
                elif kind == "progress" and on_progress is not None:
                    on_progress(float(value))

            if cancelled():
                raise TranscriptionCancelled("Transcription cancelled.")
            if error_message is not None:
                raise TranscriptionError(error_message)
            if proc.exitcode != 0:
                raise TranscriptionError(
                    f"Whisper worker exited unexpectedly with code {proc.exitcode}."
                )
            if not done or not output_path.exists():
                raise TranscriptionError("Whisper worker finished without a transcript.")

            return Transcript.load(output_path)
        finally:
            if proc.is_alive():
                _terminate_process(proc)
            updates.close()


def transcribe(
    audio: Path,
    settings: WhisperSettings | None = None,
    *,
    duration_s: float | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Transcript:
    """Transcribe audio, using a killable child process when cancellation is enabled."""
    settings = settings or WhisperSettings()
    if cancelled is None:
        return _transcribe_direct(
            audio,
            settings,
            duration_s=duration_s,
            on_progress=on_progress,
            cancelled=None,
        )

    if cancelled():
        raise TranscriptionCancelled("Transcription cancelled.")

    return _transcribe_isolated(
        audio,
        settings,
        duration_s=duration_s,
        on_progress=on_progress,
        cancelled=cancelled,
    )


# --------------------------------------------------------------------------
# Diarization
# --------------------------------------------------------------------------


def _diarize_direct(
    audio: Path,
    transcript: Transcript,
    *,
    hf_token: str | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> Transcript:
    """Label each word with a speaker, in place.

    Uses pyannote via WhisperX. Returns the transcript unchanged (with a logged
    warning) if diarization isn't available — speaker labels improve reframing
    but are never required for the pipeline to complete.

    Preconditions:
        transcript already has word-level timings.
    """
    if not transcript.words:
        return transcript

    pipeline_cls = _load_diarization_pipeline()
    if pipeline_cls is None:
        log.warning(
            "WhisperX is not installed; skipping diarization. "
            "Install it with `uv pip install 'autoclip[diarization]'`."
        )
        return transcript

    if not hf_token:
        log.warning(
            "Diarization needs a HuggingFace token. Set one with "
            "`autoclip config set-secret huggingface_token`, and accept the pyannote "
            "model licences on huggingface.co. Continuing without speaker labels."
        )
        return transcript

    device = report().gpu.device
    try:
        pipeline = pipeline_cls(use_auth_token=hf_token, device=device)
        diarization = pipeline(str(audio), min_speakers=min_speakers, max_speakers=max_speakers)
    except Exception as exc:
        log.warning("Diarization failed (%s); continuing without speaker labels.", exc)
        return transcript

    turns = _diarization_turns(diarization)
    if not turns:
        return transcript

    _assign_speakers(transcript, turns)
    log.info("Diarization labelled %d speakers.", len(transcript.speakers))
    return transcript


def _diarize_worker(
    audio: str,
    transcript_path: str,
    output_path: str,
    hf_token: str | None,
    min_speakers: int | None,
    max_speakers: int | None,
    updates,
) -> None:
    transcript = Transcript.load(Path(transcript_path))
    try:
        result = _diarize_direct(
            Path(audio),
            transcript,
            hf_token=hf_token,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
        )
        result.save(Path(output_path))
    except BaseException as exc:
        updates.put(("error", str(exc)))
        return

    updates.put(("done", ""))


def diarize(
    audio: Path,
    transcript: Transcript,
    *,
    hf_token: str | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Transcript:
    """Label speakers, using a killable worker when cancellation is enabled."""
    if cancelled is None:
        return _diarize_direct(
            audio,
            transcript,
            hf_token=hf_token,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
        )
    if cancelled():
        raise TranscriptionCancelled("Diarization cancelled.")

    context = multiprocessing.get_context("spawn")
    updates, worker_updates = context.Pipe(duplex=False)
    error_message: str | None = None
    done = False

    with tempfile.TemporaryDirectory(prefix="autoclip-diarize-") as temp_dir:
        temp_root = Path(temp_dir)
        input_path = temp_root / "input.json"
        output_path = temp_root / "output.json"
        transcript.save(input_path)

        proc = context.Process(
            target=_diarize_worker,
            args=(
                str(audio),
                str(input_path),
                str(output_path),
                hf_token,
                min_speakers,
                max_speakers,
                worker_updates,
            ),
            name="autoclip-diarize",
            daemon=True,
        )
        proc.start()
        worker_updates.close()

        try:
            while proc.is_alive():
                if cancelled():
                    _terminate_process(proc)
                    raise TranscriptionCancelled("Diarization cancelled.")
                if updates.poll(0.1):
                    try:
                        kind, value = updates.recv()
                    except (EOFError, OSError):
                        kind = ""
                        value = ""
                    if kind == "error":
                        error_message = str(value)
                    elif kind == "done":
                        done = True

            proc.join()
            while updates.poll():
                try:
                    kind, value = updates.recv()
                except (EOFError, OSError):
                    break
                if kind == "error":
                    error_message = str(value)
                elif kind == "done":
                    done = True

            if cancelled():
                raise TranscriptionCancelled("Diarization cancelled.")
            if error_message is not None:
                # Preserve the old diarization behavior: failure is non-fatal.
                log.warning(
                    "Diarization failed (%s); continuing without speaker labels.",
                    error_message,
                )
                return transcript
            if proc.exitcode != 0 or not done or not output_path.exists():
                log.warning(
                    "Diarization worker ended unexpectedly; continuing without speaker labels."
                )
                return transcript

            return Transcript.load(output_path)
        finally:
            if proc.is_alive():
                _terminate_process(proc)
            updates.close()


def _load_diarization_pipeline():
    """Import WhisperX's diarization pipeline across its API reshuffles."""
    try:
        from whisperx.diarize import DiarizationPipeline

        return DiarizationPipeline
    except ImportError:
        pass
    try:
        from whisperx import DiarizationPipeline  # type: ignore[attr-defined]

        return DiarizationPipeline
    except (ImportError, AttributeError):
        return None


def _diarization_turns(diarization) -> list[tuple[str, float, float]]:
    """Normalise WhisperX output into ``(speaker, start, end)`` tuples.

    Recent versions return a pandas DataFrame; older ones return a pyannote
    ``Annotation``. Both are handled so the extra can be upgraded independently.
    """
    turns: list[tuple[str, float, float]] = []

    if hasattr(diarization, "itertuples"):  # DataFrame
        for row in diarization.itertuples():
            speaker = getattr(row, "speaker", None)
            start = getattr(row, "start", None)
            end = getattr(row, "end", None)
            if speaker is not None and start is not None and end is not None:
                turns.append((str(speaker), float(start), float(end)))
        return turns

    if hasattr(diarization, "itertracks"):  # pyannote Annotation
        for segment, _, speaker in diarization.itertracks(yield_label=True):
            turns.append((str(speaker), float(segment.start), float(segment.end)))

    return turns


def _assign_speakers(transcript: Transcript, turns: list[tuple[str, float, float]]) -> None:
    """Attach a speaker to every word by maximum temporal overlap.

    Overlap rather than midpoint containment: words straddling a turn boundary
    are common, and the speaker who covers more of the word is the better guess.
    """
    turns = sorted(turns, key=lambda t: t[1])

    for word in transcript.words:
        best_speaker: str | None = None
        best_overlap = 0.0
        for speaker, start, end in turns:
            if start > word.end:
                break
            overlap = min(word.end, end) - max(word.start, start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = speaker
        word.speaker = best_speaker

    for segment in transcript.segments:
        segment.speaker = transcript.dominant_speaker(segment.first_word, segment.last_word)
