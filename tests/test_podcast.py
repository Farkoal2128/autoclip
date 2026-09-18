"""Podcast continuation planning and rendering helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from autoclip.pipeline import podcast
from autoclip.pipeline.prepare import Silence
from autoclip.pipeline.transcript import Transcript, Word
from autoclip.providers import PodcastCutCandidate


def test_long_silences_are_shortened_but_keep_a_natural_pause() -> None:
    cuts = podcast.silence_ranges_to_cuts(
        [Silence(start=10.0, end=13.0), Silence(start=20.0, end=20.8)],
        source_duration_s=30.0,
        threshold_s=1.5,
        keep_s=0.25,
    )

    assert cuts == [
        podcast.PodcastCut(
            start_s=10.25,
            end_s=12.75,
            kind="silence",
            reason="Long silent gap",
        )
    ]


def test_overlapping_ai_and_silence_cuts_merge_as_mixed() -> None:
    cuts = podcast.merge_cuts(
        [
            podcast.PodcastCut(10.0, 20.0, "boring", "Repeated setup"),
            podcast.PodcastCut(19.0, 22.0, "silence", "Long silent gap"),
        ],
        60.0,
    )

    assert len(cuts) == 1
    assert cuts[0].start_s == pytest.approx(10.0)
    assert cuts[0].end_s == pytest.approx(22.0)
    assert cuts[0].kind == "mixed"


def test_overlapping_window_candidates_are_deduplicated() -> None:
    candidates = [
        PodcastCutCandidate(
            start_word_index=100,
            end_word_index=200,
            reason="Repeated point",
        ),
        PodcastCutCandidate(
            start_word_index=110,
            end_word_index=195,
            reason="Same repeated point",
        ),
        PodcastCutCandidate(
            start_word_index=300,
            end_word_index=360,
            reason="Technical setup",
        ),
    ]

    deduped = podcast.dedupe_candidates(candidates)

    assert [(item.start_word_index, item.end_word_index) for item in deduped] == [
        (100, 200),
        (300, 360),
    ]


def test_ai_cut_boundaries_can_snap_to_nearby_silence() -> None:
    transcript = Transcript(
        words=[
            Word(text=f"w{i}", start=float(i), end=float(i) + 0.5)
            for i in range(20)
        ]
    )
    candidate = PodcastCutCandidate(
        start_word_index=5,
        end_word_index=12,
        reason="Low-information tangent",
    )

    cuts = podcast.boring_sections_to_cuts(
        transcript,
        [candidate],
        [Silence(start=4.6, end=5.2), Silence(start=12.2, end=13.0)],
    )

    assert len(cuts) == 1
    assert cuts[0].start_s == pytest.approx(4.9)
    assert cuts[0].end_s == pytest.approx(12.6)
    assert cuts[0].kind == "boring"


def test_render_uses_filter_script_and_original_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "stream.mp4"
    source.write_bytes(b"source")
    destination = tmp_path / "exports" / "podcast.mp3"
    work = tmp_path / "work"
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"mp3")

    monkeypatch.setattr(podcast.ffmpeg, "run", fake_run)

    result = podcast.render_podcast(
        source,
        destination,
        [podcast.PodcastCut(10.0, 20.0, "boring", "Repeated")],
        source_duration_s=60.0,
        output_duration_s=50.0,
        loudness_lufs=-14.0,
        work_dir=work,
    )

    assert result == destination
    args = captured["args"]
    assert str(source) in args
    assert "-filter_complex_script" in args
    assert "libmp3lame" in args
    script = (work / "podcast-filter.txt").read_text(encoding="utf-8")
    assert "between(t\\,10.0000\\,20.0000)" in script
