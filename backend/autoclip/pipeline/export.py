"""Export stage — crop path plus captions plus audio normalisation to an MP4.

The whole render is a single ffmpeg pass. Per-shot crop segments are trimmed out
of the source, cropped independently, scaled to a common output size, and
concatenated in the filtergraph — which is what allows a wide two-shot and a
tight single to sit in one clip without a mid-stream frame-size change.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ExportSettings
from ..system import report
from . import captions as captions_module
from . import ffmpeg
from .captions import CaptionStyle
from .reframe.croppath import CropPath, segment_crop_filter
from .transcript import Word

log = logging.getLogger(__name__)

#: Output dimensions per aspect ratio. 1080-wide is the platform sweet spot:
#: high enough to avoid re-encode mush, low enough to upload quickly.
RATIOS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}

#: Loudness targets. -14 LUFS integrated with -1.5 dBTP is what every major
#: platform normalises toward, so hitting it ourselves avoids their processing.
LOUDNESS_TRUE_PEAK = -1.5
LOUDNESS_RANGE = 11.0

AUDIO_BITRATE = "192k"
AUDIO_SAMPLE_RATE = 48_000
MANUAL_LAYOUT_GLIDE_FPS = 60


class ExportError(RuntimeError):
    """Rendering a clip failed."""


@dataclass(frozen=True)
class LayoutRect:
    x: float
    y: float
    width: float
    height: float

    @classmethod
    def from_dict(cls, data: dict) -> "LayoutRect":
        return cls(
            x=float(data["x"]),
            y=float(data["y"]),
            width=float(data["width"]),
            height=float(data["height"]),
        )


@dataclass(frozen=True)
class LayoutRegion:
    id: str
    label: str
    source: LayoutRect
    destination: LayoutRect

    @classmethod
    def from_dict(cls, data: dict) -> "LayoutRegion":
        return cls(
            id=str(data["id"]),
            label=str(data.get("label") or ""),
            source=LayoutRect.from_dict(data["source"]),
            destination=LayoutRect.from_dict(data["destination"]),
        )


@dataclass(frozen=True)
class LayoutFrame:
    base_center_x: float = 0.5
    base_center_y: float = 0.5
    overlays: tuple[LayoutRegion, ...] = ()

    @classmethod
    def from_dict(cls, data: dict) -> "LayoutFrame":
        return cls(
            base_center_x=float(data.get("base_center_x", 0.5)),
            base_center_y=float(data.get("base_center_y", 0.5)),
            overlays=tuple(LayoutRegion.from_dict(item) for item in data.get("overlays", [])),
        )


@dataclass(frozen=True)
class LayoutCue:
    id: str
    at_s: float
    transition: str = "cut"
    lead_s: float = 1.0
    layout: LayoutFrame = field(default_factory=LayoutFrame)

    @classmethod
    def from_dict(cls, data: dict) -> "LayoutCue":
        return cls(
            id=str(data.get("id") or ""),
            at_s=float(data.get("at_s", 0.0)),
            transition=str(data.get("transition") or "cut"),
            lead_s=max(0.0, float(data.get("lead_s", 1.0))),
            layout=LayoutFrame.from_dict(data.get("layout") or {}),
        )


@dataclass(frozen=True)
class ManualLayout(LayoutFrame):
    cues: tuple[LayoutCue, ...] = ()

    @classmethod
    def from_dict(cls, data: dict) -> "ManualLayout":
        base = LayoutFrame.from_dict(data)
        return cls(
            base_center_x=base.base_center_x,
            base_center_y=base.base_center_y,
            overlays=base.overlays,
            cues=tuple(
                sorted(
                    (LayoutCue.from_dict(item) for item in data.get("cues", [])),
                    key=lambda cue: cue.at_s,
                )
            ),
        )


@dataclass
class ExportRequest:
    source: Path
    destination: Path
    start_s: float
    end_s: float
    crop_path: CropPath
    words: list[Word]
    style: CaptionStyle
    ratio: str = "9:16"
    burn_captions: bool = True
    cuts: list[tuple[float, float]] = field(default_factory=list)
    layout: ManualLayout | None = None

    @property
    def source_duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def normalised_cuts(self) -> list[tuple[float, float]]:
        return _normalise_cuts(self.start_s, self.end_s, self.cuts)

    @property
    def relative_cuts(self) -> list[tuple[float, float]]:
        return [
            (start - self.start_s, end - self.start_s)
            for start, end in self.normalised_cuts
        ]

    @property
    def duration_s(self) -> float:
        removed = sum(end - start for start, end in self.normalised_cuts)
        return max(0.0, self.source_duration_s - removed)


def ratio_dimensions(ratio: str) -> tuple[int, int]:
    if ratio not in RATIOS:
        raise ExportError(f"Unknown ratio {ratio!r}. Available: {', '.join(RATIOS)}")
    return RATIOS[ratio]


def slugify_title(title: str, *, max_length: int = 50) -> str:
    """Filename-safe slug for a clip title."""
    slug = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug[:max_length] or "clip"


def output_filename(title: str, ratio: str) -> str:
    return f"{slugify_title(title)}_{ratio.replace(':', 'x')}.mp4"


def _normalise_cuts(
    clip_start: float, clip_end: float, cuts: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    clipped = [
        (max(clip_start, float(start)), min(clip_end, float(end)))
        for start, end in cuts
        if min(clip_end, float(end)) - max(clip_start, float(start)) > 0.001
    ]
    clipped.sort()
    merged: list[tuple[float, float]] = []
    for start, end in clipped:
        if merged and start <= merged[-1][1] + 0.001:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _keep_expression(cuts: list[tuple[float, float]]) -> str:
    removed = "+".join(f"between(t\\,{start:.4f}\\,{end:.4f})" for start, end in cuts)
    return f"not({removed})"


def _video_timestamp_expression(cuts: list[tuple[float, float]]) -> str:
    shifts = "+".join(
        f"gte(PTS*TB\\,{end:.4f})*{end - start:.4f}" for start, end in cuts
    )
    return f"PTS-STARTPTS-({shifts})/TB"


def retime_words_for_cuts(
    words: list[Word], cuts: list[tuple[float, float]]
) -> list[Word]:
    """Drop words intersecting a cut and shift later word timings left."""
    if not cuts:
        return list(words)

    retimed: list[Word] = []
    for word in words:
        if any(word.start < end and word.end > start for start, end in cuts):
            continue
        shift = sum(end - start for start, end in cuts if end <= word.start)
        retimed.append(
            Word(
                text=word.text,
                start=word.start - shift,
                end=word.end - shift,
                speaker=word.speaker,
            )
        )
    return retimed


# --------------------------------------------------------------------------
# Filtergraph construction
# --------------------------------------------------------------------------


def build_video_filtergraph(
    request: ExportRequest,
    *,
    subtitle_name: str | None,
    fonts_name: str = "fonts",
) -> str:
    """Build the ``-filter_complex`` video chain for one clip."""
    out_w, out_h = ratio_dimensions(request.ratio)

    if request.layout is not None:
        if request.ratio not in ("9:16", "1:1"):
            raise ExportError("Custom crop layouts are supported only for 9:16 and 1:1.")
        parts, current = _build_manual_layout_chain(request, out_w, out_h)
    else:
        parts, current = _build_auto_crop_chain(request, out_w, out_h)

    if request.relative_cuts:
        parts.append(
            f"{current}select='{_keep_expression(request.relative_cuts)}',"
            f"setpts={_video_timestamp_expression(request.relative_cuts)}[vcut]"
        )
        current = "[vcut]"

    if request.burn_captions and subtitle_name is not None:
        # Bare relative names — ffmpeg runs with its cwd set to the render
        # workspace, so there is nothing here that needs escaping.
        parts.append(f"{current}ass=filename={subtitle_name}:fontsdir={fonts_name}[vout]")
    else:
        parts.append(f"{current}null[vout]")

    return ";".join(parts)


def _build_auto_crop_chain(
    request: ExportRequest, out_w: int, out_h: int
) -> tuple[list[str], str]:
    segments = request.crop_path.segments
    if not segments:
        raise ExportError("The crop path has no segments.")

    parts: list[str] = []
    labels: list[str] = []

    for index, segment in enumerate(segments):
        label = f"v{index}"
        labels.append(f"[{label}]")

        if len(segments) == 1:
            source_label = "[0:v]"
        else:
            source_label = f"[s{index}]"
            parts.append(
                f"[0:v]trim=start={segment.start_s:.4f}:end={segment.end_s:.4f},"
                f"setpts=PTS-STARTPTS{source_label}"
            )

        if segment.fit:
            parts.extend(_fit_chain(source_label, index, out_w, out_h))
            continue

        chain = [segment_crop_filter(segment)]
        if segment.zoom > 0:
            chain.append(_zoom_filter(segment.zoom, out_w, out_h))
        chain.append(f"scale={out_w}:{out_h}:flags=lanczos")
        chain.append("setsar=1,format=yuv420p")
        parts.append(f"{source_label}{','.join(chain)}[{label}]")

    if len(segments) == 1:
        return parts, "[v0]"

    parts.append(f"{''.join(labels)}concat=n={len(segments)}:v=1:a=0[vcat]")
    return parts, "[vcat]"


def _build_manual_layout_chain(
    request: ExportRequest, out_w: int, out_h: int
) -> tuple[list[str], str]:
    """Compose timestamped manual layouts, with optional base-crop glides."""
    layout = request.layout
    if layout is None:
        raise ExportError("Manual layout chain requested without a layout.")

    source_w = request.crop_path.source_width
    source_h = request.crop_path.source_height
    if source_w <= 0 or source_h <= 0:
        raise ExportError("Source dimensions are required for a custom crop layout.")

    duration = request.source_duration_s
    cues = [
        cue
        for cue in layout.cues
        if request.start_s < cue.at_s < request.end_s
    ]

    frames: list[LayoutFrame] = [
        LayoutFrame(layout.base_center_x, layout.base_center_y, layout.overlays)
    ]
    boundaries = [0.0]
    transitions: list[LayoutCue] = []
    for cue in cues:
        boundaries.append(cue.at_s - request.start_s)
        transitions.append(cue)
        frames.append(cue.layout)
    boundaries.append(duration)

    # If the clip starts after one or more stored cues, inherit the most recent
    # snapshot so trimming a clip does not reset its composition.
    previous = [
        cue for cue in layout.cues if cue.at_s <= request.start_s
    ]
    if previous:
        frames[0] = previous[-1].layout

    total_branches = sum(1 + len(frame.overlays) for frame in frames)
    smooth_glide = any(cue.transition == "glide" and cue.lead_s > 0 for cue in cues)
    parts: list[str] = []

    branch_labels = [f"[layoutin{index}]" for index in range(total_branches)]
    if total_branches == 1:
        branch_labels = ["[0:v]"]
    else:
        parts.append(f"[0:v]split={total_branches}{''.join(branch_labels)}")

    branch_index = 0
    outputs: list[str] = []
    for index, frame in enumerate(frames):
        seg_start = boundaries[index]
        seg_end = boundaries[index + 1]
        seg_duration = max(0.0001, seg_end - seg_start)
        next_cue = transitions[index] if index < len(transitions) else None

        base_input = branch_labels[branch_index]
        branch_index += 1
        base_trim = f"[layoutbasein{index}]"
        base_timing = "setpts=PTS-STARTPTS"
        if smooth_glide:
            # Generate crop positions at 60 fps even when the source is 24/30 fps.
            # The source frames may repeat, but the virtual camera keeps moving
            # every output frame instead of jumping only on source-frame ticks.
            base_timing += f",fps={MANUAL_LAYOUT_GLIDE_FPS}"
        parts.append(
            f"{base_input}trim=start={seg_start:.4f}:end={seg_end:.4f},"
            f"{base_timing}{base_trim}"
        )

        crop_w, crop_h, max_x, max_y = _base_crop_geometry(
            source_w, source_h, out_w, out_h
        )
        x_expr = _layout_axis_expression(
            max_x,
            frame.base_center_x,
            next_cue.layout.base_center_x if next_cue else frame.base_center_x,
            seg_duration,
            next_cue,
        )
        y_expr = _layout_axis_expression(
            max_y,
            frame.base_center_y,
            next_cue.layout.base_center_y if next_cue else frame.base_center_y,
            seg_duration,
            next_cue,
        )
        base_label = f"[layoutbase{index}]"
        parts.append(
            f"{base_trim}crop={crop_w}:{crop_h}:x='{x_expr}':y='{y_expr}',"
            f"scale={out_w}:{out_h}:flags=lanczos,setsar=1,format=yuv420p{base_label}"
        )
        current = base_label

        overlay_fade = _overlay_fade_timing(seg_duration, next_cue)
        for overlay_index, region in enumerate(frame.overlays):
            overlay_input = branch_labels[branch_index]
            branch_index += 1
            overlay_trim = f"[layoutoverlayin{index}_{overlay_index}]"
            overlay_timing = "setpts=PTS-STARTPTS"
            if overlay_fade is not None:
                # Fade outgoing regions at the same 60 fps cadence as the base glide.
                overlay_timing += f",fps={MANUAL_LAYOUT_GLIDE_FPS}"
            parts.append(
                f"{overlay_input}trim=start={seg_start:.4f}:end={seg_end:.4f},"
                f"{overlay_timing}{overlay_trim}"
            )
            sx, sy, sw, sh = _normalised_source_rect(region.source, source_w, source_h)
            dx, dy, dw, dh = _normalised_destination_rect(region.destination, out_w, out_h)
            overlay_label = f"[layoutoverlay{index}_{overlay_index}]"
            overlay_filters = [
                f"crop={sw}:{sh}:{sx}:{sy}",
                f"scale={dw}:{dh}:flags=lanczos",
                "setsar=1",
            ]
            if overlay_fade is not None:
                fade_start, fade_duration = overlay_fade
                overlay_filters.extend(
                    [
                        "format=yuva420p",
                        (
                            f"fade=t=out:st={fade_start:.4f}:d={fade_duration:.4f}:"
                            "alpha=1"
                        ),
                    ]
                )
            else:
                overlay_filters.append("format=yuv420p")
            parts.append(f"{overlay_trim}{','.join(overlay_filters)}{overlay_label}")
            next_label = f"[layoutcomposed{index}_{overlay_index}]"
            parts.append(
                f"{current}{overlay_label}overlay=x={dx}:y={dy}:"
                f"eof_action=pass:shortest=1{next_label}"
            )
            current = next_label

        outputs.append(current)

    if len(outputs) == 1:
        return parts, outputs[0]

    parts.append(f"{''.join(outputs)}concat=n={len(outputs)}:v=1:a=0[layoutcat]")
    return parts, "[layoutcat]"


def _overlay_fade_timing(
    segment_duration: float,
    cue: LayoutCue | None,
) -> tuple[float, float] | None:
    if cue is None or cue.transition != "glide" or cue.lead_s <= 0:
        return None
    duration = min(segment_duration, cue.lead_s)
    if duration <= 0:
        return None
    return max(0.0, segment_duration - duration), duration


def _base_crop_geometry(
    source_w: int, source_h: int, out_w: int, out_h: int
) -> tuple[int, int, int, int]:
    target_ratio = out_w / out_h
    source_ratio = source_w / source_h

    if source_ratio >= target_ratio:
        crop_h = _even(source_h)
        crop_w = _even(crop_h * target_ratio)
    else:
        crop_w = _even(source_w)
        crop_h = _even(crop_w / target_ratio)

    return crop_w, crop_h, max(0, source_w - crop_w), max(0, source_h - crop_h)


def _layout_axis_expression(
    maximum: int,
    start_center: float,
    end_center: float,
    segment_duration: float,
    cue: LayoutCue | None,
) -> str:
    start = maximum * max(0.0, min(1.0, start_center))
    end = maximum * max(0.0, min(1.0, end_center))
    if cue is None or cue.transition != "glide" or cue.lead_s <= 0 or abs(end - start) < 0.01:
        return f"{start:.4f}"

    lead = min(segment_duration, cue.lead_s)
    glide_start = max(0.0, segment_duration - lead)
    progress = f"((t-{glide_start:.4f})/{lead:.4f})"
    # Quintic smootherstep: zero velocity and zero acceleration at both ends.
    # This avoids the mechanical start/stop of a linear camera pan.
    eased = (
        f"{progress}*{progress}*{progress}*"
        f"({progress}*({progress}*6-15)+10)"
    )
    expr = (
        f"if(lt(t,{glide_start:.4f}),{start:.4f},"
        f"{start:.4f}+({end - start:.4f})*({eased}))"
    )
    return expr.replace(",", "\\,")

def _even(value: float, *, minimum: int = 2) -> int:
    rounded = max(minimum, int(round(value)))
    return rounded if rounded % 2 == 0 else rounded - 1


def _base_crop_rect(
    source_w: int,
    source_h: int,
    out_w: int,
    out_h: int,
    center_x: float,
    center_y: float,
) -> tuple[int, int, int, int]:
    target_ratio = out_w / out_h
    source_ratio = source_w / source_h

    if source_ratio >= target_ratio:
        crop_h = _even(source_h)
        crop_w = _even(crop_h * target_ratio)
    else:
        crop_w = _even(source_w)
        crop_h = _even(crop_w / target_ratio)

    max_x = max(0, source_w - crop_w)
    max_y = max(0, source_h - crop_h)
    x = _even(max_x * max(0.0, min(1.0, center_x)), minimum=0)
    y = _even(max_y * max(0.0, min(1.0, center_y)), minimum=0)
    return min(x, max_x), min(y, max_y), crop_w, crop_h


def _normalised_source_rect(
    rect: LayoutRect, source_w: int, source_h: int
) -> tuple[int, int, int, int]:
    width = min(_even(source_w * rect.width), _even(source_w))
    height = min(_even(source_h * rect.height), _even(source_h))
    max_x = max(0, source_w - width)
    max_y = max(0, source_h - height)
    x = min(_even(source_w * rect.x, minimum=0), max_x)
    y = min(_even(source_h * rect.y, minimum=0), max_y)
    return x, y, width, height


def _normalised_destination_rect(
    rect: LayoutRect, out_w: int, out_h: int
) -> tuple[int, int, int, int]:
    width = min(_even(out_w * rect.width), out_w)
    height = min(_even(out_h * rect.height), out_h)
    max_x = max(0, out_w - width)
    max_y = max(0, out_h - height)
    x = min(_even(out_w * rect.x, minimum=0), max_x)
    y = min(_even(out_h * rect.y, minimum=0), max_y)
    return x, y, width, height


def _fit_chain(source_label: str, index: int, out_w: int, out_h: int) -> list[str]:
    """Fit the whole frame into the output over a blurred copy of itself.

    Used when the subjects are spread wider than any crop can hold. Plain black
    bars would be honest but look cheap on a phone; a blurred fill is what
    viewers are used to seeing and keeps the frame full-bleed.
    """
    return [
        f"{source_label}split=2[fitbg{index}][fitfg{index}]",
        (
            f"[fitbg{index}]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},boxblur=24:2[bg{index}]"
        ),
        (
            f"[fitfg{index}]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:"
            f"flags=lanczos[fg{index}]"
        ),
        (f"[bg{index}][fg{index}]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v{index}]"),
    ]


def _zoom_filter(zoom: float, out_w: int, out_h: int) -> str:
    """A slow push-in over the segment.

    Uses ``zoompan``, which is the only filter that can change apparent scale
    while holding a fixed output size. Off by default — at very small
    increments it can step visibly, and a locked frame beats a stuttering one.
    """
    end_zoom = 1.0 + zoom
    return f"zoompan=z='min(zoom+{zoom / 240:.6f},{end_zoom:.4f})':d=1:s={out_w}x{out_h}:fps=30"


def build_audio_filtergraph(
    settings: ExportSettings, cuts: list[tuple[float, float]] | None = None
) -> str:
    loudnorm = f"loudnorm=I={settings.loudness_lufs}:TP={LOUDNESS_TRUE_PEAK}:LRA={LOUDNESS_RANGE}"
    if not cuts:
        return loudnorm
    return f"aselect='{_keep_expression(cuts)}',asetpts=N/SR/TB,{loudnorm}"


def encoder_args(settings: ExportSettings) -> list[str]:
    """Pick the video encoder and its quality settings.

    NVENC is materially faster where available. Its quality knob is ``-cq``
    rather than ``-crf``, and the two scales are close enough that reusing the
    configured value keeps output consistent between machines.
    """
    if settings.prefer_hardware_encoder and report().ffmpeg.nvenc_works:
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-rc",
            "vbr",
            "-cq",
            str(settings.crf),
            "-b:v",
            "0",
            "-profile:v",
            "high",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(settings.crf),
        "-profile:v",
        "high",
    ]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def export_clip(
    request: ExportRequest,
    *,
    work_dir: Path,
    settings: ExportSettings | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Render one clip to an MP4 and return the output path.

    Preconditions:
        request.source exists; request.crop_path segments span the clip duration
        in clip-relative seconds.
    """
    settings = settings or ExportSettings()
    out_w, out_h = ratio_dimensions(request.ratio)

    # Each clip renders in its own workspace so concurrent exports can't collide
    # on the shared `captions.ass` name that the relative-path scheme requires.
    workspace = work_dir / request.destination.stem
    workspace.mkdir(parents=True, exist_ok=True)

    subtitle_name: str | None = None
    fonts_name = "fonts"
    render_cwd: Path | None = None
    render_words = retime_words_for_cuts(request.words, request.normalised_cuts)

    if request.burn_captions and render_words:
        ass_path = captions_module.write_ass(
            workspace / "captions.ass",
            render_words,
            request.style,
            width=out_w,
            height=out_h,
            time_offset_s=request.start_s,
        )
        render_cwd, subtitle_name, fonts_name = ffmpeg.relative_filter_workspace(
            ass_path, captions_module.FONT_DIR
        )

    filtergraph = build_video_filtergraph(
        request, subtitle_name=subtitle_name, fonts_name=fonts_name
    )

    request.destination.parent.mkdir(parents=True, exist_ok=True)

    # Input and output paths are ordinary argv arguments, so they stay absolute
    # and need no escaping regardless of the working directory.
    args = [
        # Seeking before -i decodes from the nearest keyframe and is far faster
        # than an output-side seek; modern ffmpeg keeps it frame-accurate.
        "-ss",
        f"{request.start_s:.4f}",
        "-t",
        f"{request.source_duration_s:.4f}",
        "-i",
        str(request.source),
        "-filter_complex",
        filtergraph,
        "-map",
        "[vout]",
        "-map",
        "0:a?",
        "-af",
        build_audio_filtergraph(settings, request.relative_cuts),
        *encoder_args(settings),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-movflags",
        "+faststart",
        str(request.destination),
    ]

    try:
        ffmpeg.run(
            args,
            total_duration_s=request.duration_s,
            on_progress=on_progress,
            cancelled=cancelled,
            cwd=render_cwd,
        )
    except ffmpeg.FFmpegError as exc:
        raise ExportError(f"Could not render {request.destination.name}.\n{exc}") from exc

    if not request.destination.exists() or request.destination.stat().st_size == 0:
        raise ExportError(f"ffmpeg reported success but {request.destination.name} is empty.")

    if settings.write_srt and render_words:
        captions_module.write_srt(
            request.destination.with_suffix(".srt"),
            render_words,
            time_offset_s=request.start_s,
        )

    log.info(
        "Exported %s (%.1fs, %s)",
        request.destination.name,
        request.duration_s,
        request.ratio,
    )
    return request.destination
