"""Reusable highlight-pass metadata."""

from autoclip import reuse
from autoclip.db.models import Job


def test_rerun_metadata_accumulates_passes_and_excluded_ranges() -> None:
    first = Job(id="job-1", source_id="source", settings={})
    second_settings = reuse.build_rerun_settings(
        first,
        {"active_provider": "anthropic"},
        [(10, 20)],
    )
    second = Job(id="job-2", source_id="source", settings=second_settings)

    third_settings = reuse.build_rerun_settings(
        second,
        {"active_provider": "openai"},
        [(40, 50)],
    )
    metadata = third_settings[reuse.META_KEY]

    assert metadata["pass_number"] == 3
    assert metadata["exclude_job_ids"] == ["job-1", "job-2"]
    assert metadata["exclude_ranges"] == [[10, 20], [40, 50]]
