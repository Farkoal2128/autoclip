"""Regression tests for aspect-preserving custom layout geometry."""

from autoclip.pipeline import export


def test_overlay_source_is_center_cropped_to_destination_aspect() -> None:
    fitted = export._fit_source_rect_to_destination(
        0,
        0,
        300,
        100,
        200,
        200,
    )

    assert fitted == (100, 0, 100, 100)


def test_overlay_source_keeps_even_crop_coordinates() -> None:
    x, y, width, height = export._fit_source_rect_to_destination(
        20,
        10,
        100,
        300,
        200,
        100,
    )

    assert x % 2 == 0
    assert y % 2 == 0
    assert width % 2 == 0
    assert height % 2 == 0
    assert width / height == 2
