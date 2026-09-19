"""Regression tests for transcription-stage debug activity."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from autoclip import cuda
from autoclip.config import WhisperSettings
from autoclip.pipeline import transcribe
from autoclip.pipeline.transcript import Segment, Transcript, Word


def test_whisper_reports_model_language_and_decode_milestones(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = types.ModuleType("faster_whisper")

    class FakeWhisperModel:
        def __init__(self, model: str, *, device: str, compute_type: str) -> None:
            assert model == "small"
            assert device == "cuda"
            assert compute_type == "float16"

        def transcribe(self, _audio: str, **_kwargs):
            info = SimpleNamespace(
                language="en",
                language_probability=0.987,
                duration=100.0,
            )
            segments = [
                SimpleNamespace(
                    text="one",
                    start=0.0,
                    end=20.0,
                    words=[SimpleNamespace(word=" one", start=0.0, end=1.0)],
                ),
                SimpleNamespace(
                    text="two",
                    start=20.0,
                    end=60.0,
                    words=[SimpleNamespace(word=" two", start=20.0, end=21.0)],
                ),
                SimpleNamespace(
                    text="three",
                    start=60.0,
                    end=100.0,
                    words=[SimpleNamespace(word=" three", start=60.0, end=61.0)],
                ),
            ]
            return iter(segments), info

    module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setattr(cuda, "ensure_cuda_libraries", lambda: None)
    monkeypatch.setattr(
        transcribe,
        "resolve_compute",
        lambda _settings: ("cuda", "float16"),
    )

    statuses: list[str] = []
    progress: list[float] = []
    result = transcribe._transcribe_direct(
        tmp_path / "audio.wav",
        WhisperSettings(model="small"),
        duration_s=100.0,
        on_progress=progress.append,
        on_status=statuses.append,
    )

    assert len(result.words) == 3
    assert progress[-1] == pytest.approx(1.0)
    assert statuses[0] == "Preparing Whisper runtime"
    assert "Whisper runtime ready" in statuses
    assert any(
        "model=small" in message
        and "device=cuda" in message
        and "compute=float16" in message
        for message in statuses
    )
    assert "Whisper small model loaded on cuda" in statuses
    assert any(
        "Detected language en" in message and "98.7% confidence" in message
        for message in statuses
    )
    assert "Decoding speech · VAD enabled · word timestamps enabled" in statuses
    assert any(
        "Decoded 1:00 / 1:40" in message
        and "60%" in message
        and "2 words" in message
        for message in statuses
    )
    assert statuses[-1] == "Whisper decoding complete · 3 words · 3 segments"


def test_diarization_reports_model_and_speaker_milestones(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeDiarization:
        def itertracks(self, yield_label: bool):
            assert yield_label is True
            yield SimpleNamespace(start=0.0, end=1.0), None, "SPEAKER_00"
            yield SimpleNamespace(start=1.0, end=2.0), None, "SPEAKER_01"

    class FakePipeline:
        def __init__(self, *, use_auth_token: str, device: str) -> None:
            assert use_auth_token == "hf-test"
            assert device == "cuda"

        def __call__(self, _audio: str, **_kwargs):
            return FakeDiarization()

    transcript = Transcript(
        words=[
            Word(text="hello", start=0.0, end=0.8),
            Word(text="there", start=1.1, end=1.8),
        ],
        segments=[
            Segment(
                text="hello there",
                start=0.0,
                end=2.0,
                first_word=0,
                last_word=1,
            )
        ],
    )

    monkeypatch.setattr(transcribe, "_load_diarization_pipeline", lambda: FakePipeline)
    monkeypatch.setattr(
        transcribe,
        "report",
        lambda: SimpleNamespace(gpu=SimpleNamespace(device="cuda")),
    )

    statuses: list[str] = []
    result = transcribe._diarize_direct(
        tmp_path / "audio.wav",
        transcript,
        hf_token="hf-test",
        on_status=statuses.append,
    )

    assert result.speakers == ["SPEAKER_00", "SPEAKER_01"]
    assert statuses == [
        "Preparing speaker diarization",
        "Loading speaker diarization model on cuda",
        "Speaker model loaded · analyzing speaker turns",
        "Assigning speaker labels · 2 speaker turns",
        "Speaker diarization complete · 2 speakers",
    ]
