"""Follow-up highlight-pass exclusion behavior."""

from autoclip.pipeline.highlights import overlaps_excluded


def test_followup_rejects_shifted_near_duplicate() -> None:
    assert overlaps_excluded(110, 190, [(100, 200)]) is True


def test_followup_allows_small_boundary_overlap_for_a_different_moment() -> None:
    assert overlaps_excluded(190, 280, [(100, 200)]) is False
