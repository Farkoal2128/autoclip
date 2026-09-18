from pathlib import Path

from autoclip.pipeline import captions
from autoclip.pipeline.export import (
    ExportRequest,
    LayoutCue,
    LayoutFrame,
    LayoutRect,
    LayoutRegion,
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


def test_layout_cue_default_lead_is_one_second() -> None:
    cue = LayoutCue(id="default", at_s=105.0)
    assert cue.lead_s == 1.0


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
    assert "setpts=PTS-STARTPTS,fps=60[layoutbasein0]" in graph
    assert "[layoutcat]fps=60[vglidefps]" in graph
    assert "*6-15)+10" in graph
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
    assert "fps=60" not in graph
    assert "[vglidefps]" not in graph
    assert "concat=n=2:v=1:a=0[layoutcat]" in graph


def test_glide_fades_previous_overlays_for_the_full_lead_time() -> None:
    region = LayoutRegion(
        id="vtuber",
        label="VTuber",
        source=LayoutRect(x=0.7, y=0.0, width=0.3, height=1.0),
        destination=LayoutRect(x=0.65, y=0.65, width=0.3, height=0.3),
    )
    layout = ManualLayout(
        base_center_x=0.0,
        base_center_y=0.5,
        overlays=(region,),
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

    assert "setpts=PTS-STARTPTS,fps=60[layoutoverlayin0_0]" in graph
    assert "format=yuva420p" in graph
    assert "fade=t=out:st=8.0000:d=2.0000:alpha=1" in graph


def test_glide_lead_is_clamped_to_current_layout_segment() -> None:
    layout = ManualLayout(
        base_center_x=0.0,
        base_center_y=0.5,
        cues=(
            LayoutCue(
                id="first",
                at_s=105.0,
                transition="cut",
                layout=LayoutFrame(base_center_x=0.25, base_center_y=0.5),
            ),
            LayoutCue(
                id="second",
                at_s=106.0,
                transition="glide",
                lead_s=5.0,
                layout=LayoutFrame(base_center_x=1.0, base_center_y=0.5),
            ),
        ),
    )

    graph = build_video_filtergraph(_request(layout), subtitle_name=None)

    assert "trim=start=5.0000:end=6.0000" in graph
    assert "if(lt(t\\,0.0000)" in graph
    assert "setpts=PTS-STARTPTS,fps=60[layoutbasein1]" in graph
    assert "((t-0.0000)/1.0000)" in graph

