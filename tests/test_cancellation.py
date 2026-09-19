"""Cancellation regression tests for long-running pipeline work."""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

import pytest
from autoclip.pipeline import transcribe


def test_pre_cancelled_transcription_does_not_start_whisper() -> None:
    with pytest.raises(transcribe.TranscriptionCancelled):
        transcribe.transcribe(
            Path("unused.wav"),
            cancelled=lambda: True,
        )


def test_whisper_worker_can_be_force_stopped() -> None:
    context = multiprocessing.get_context("spawn")
    proc = context.Process(target=time.sleep, args=(60,))
    proc.start()

    transcribe._terminate_process(proc)

    assert not proc.is_alive()
    assert proc.exitcode is not None
