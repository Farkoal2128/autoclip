from pathlib import Path

from autoclip.pipeline import captions
from autoclip.pipeline.export import (
    ExportRequest,
    LayoutCue,
    LayoutFrame,
    ManualLayout,
    build_video_filtergraph,
)
from autoclip.pipeline.reframe.croppath import CropPath


def _request(layout: ManualLayout) -> ExportRequest:
    return ExportRequest(
        source=Path("source.mp4"),
        destination=Path("out.mp4"),
        start_s=100.0,
        end_s=120.0,
        crop_path=CropPath(source_width=1920, source_height=1080),
        words=[],
        style=captions.get_style("bold_pop"),
        ratio="9:16",
        burn_captions=False,
        layout=layout,
    )


def test_manual_layout_old_payload_still_loads() -> None:
    layout = ManualLayout.from_dict(
        {
            "base_center_x": 0.25,
            "base_center_y": 0.75,
            "overlays": [],
        }
    )

    assert layout.base_center_x == 0.25
    assert layout.base_center_y == 0.75
    assert layout.cues == ()


def test_glide_starts_lead_seconds_before_cue() -> None:
    layout = ManualLayout(
        base_center_x=0.0,
        base_center_y=0.5,
        cues=(
            LayoutCue(
                id="gameplay",
                at_s=110.0,
                transition="glide",
                lead_s=2.0,
                layout=LayoutFrame(base_center_x=1.0, base_center_y=0.5),
            ),
        ),
    )

    graph = build_video_filtergraph(_request(layout), subtitle_name=None)

    assert "trim=start=0.0000:end=10.0000" in graph
    assert "if(lt(t\\,8.0000)" in graph
    assert "trim=start=10.0000:end=20.0000" in graph


def test_cut_does_not_interpolate_base_crop() -> None:
    layout = ManualLayout(
        base_center_x=0.0,
        base_center_y=0.5,
        cues=(
            LayoutCue(
                id="chat",
                at_s=110.0,
                transition="cut",
                lead_s=5.0,
                layout=LayoutFrame(base_center_x=1.0, base_center_y=0.5),
            ),
        ),
    )

    graph = build_video_filtergraph(_request(layout), subtitle_name=None)

    assert "if(lt(t\\," not in graph
    assert "concat=n=2:v=1:a=0[layoutcat]" in graph
