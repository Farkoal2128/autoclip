"""Follow-up highlight-pass exclusion behavior."""

import pytest

from autoclip.pipeline.highlights import HighlightError, detect, overlaps_excluded
from autoclip.pipeline.transcript import Transcript, Word
from autoclip.providers.base import DetectionConfig, LLMProvider, ProviderStatus


def test_followup_rejects_shifted_near_duplicate() -> None:
    assert overlaps_excluded(110, 190, [(100, 200)]) is True


def test_followup_allows_small_boundary_overlap_for_a_different_moment() -> None:
    assert overlaps_excluded(190, 280, [(100, 200)]) is False


class EmptyLocalProvider(LLMProvider):
    name = "ollama"
    requires_key = False

    def __init__(self, response: str = '{"clips":[]}') -> None:
        super().__init__("llama3.1:8b")
        self.response = response
        self.calls = 0

    async def _complete(self, system: str, user: str, config: DetectionConfig) -> str:
        self.calls += 1
        return self.response

    async def health_check(self) -> ProviderStatus:
        return ProviderStatus(name=self.name, available=True)


def _ten_minute_transcript() -> Transcript:
    return Transcript(
        words=[
            Word(text=f"w{i}", start=float(i), end=float(i) + 0.5)
            for i in range(600)
        ]
    )


@pytest.mark.asyncio
async def test_ollama_uses_smaller_four_minute_windows() -> None:
    provider = EmptyLocalProvider()

    with pytest.raises(
        HighlightError,
        match=r"All 4 transcript window\(s\) were analyzed successfully",
    ):
        await detect(
            _ten_minute_transcript(),
            provider,
            DetectionConfig(),
            job_id="test-job",
        )

    assert provider.calls == 4


@pytest.mark.asyncio
async def test_all_failed_windows_are_not_reported_as_valid_empty() -> None:
    provider = EmptyLocalProvider("not json")

    with pytest.raises(HighlightError, match=r"failed in all 4 transcript windows"):
        await detect(
            _ten_minute_transcript(),
            provider,
            DetectionConfig(),
            job_id="test-job",
        )

    # The shared provider loop retries each malformed window once.
    assert provider.calls == 8
