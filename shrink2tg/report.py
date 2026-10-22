# -*- coding: utf-8 -*-
"""Rendering a plan as a short Hebrew explanation for the user."""
from __future__ import annotations

from typing import List

from . import planner as pl
from . import texts as T

SUB_LABEL = {"text": T.SUBS_TEXT, "burn": T.SUBS_BURN, "none": T.SUBS_NONE}
CODEC_LABEL = {
    "libx265": "H.265", "hevc_nvenc": "H.265", "hevc_qsv": "H.265", "hevc_amf": "H.265",
    "libx264": "H.264", "h264_nvenc": "H.264", "h264_qsv": "H.264", "h264_amf": "H.264",
}


def describe(plan: pl.Plan, settings) -> str:
    info = plan.info
    video = info.video
    lines: List[str] = [T.PLAN_HEADER.format(name=info.path), ""]
    if video is not None:
        lines.append(T.PLAN_SOURCE.format(
            size=pl.human_size(info.size), duration=pl.human_time(info.duration),
            width=video.width, height=video.height, fps=video.fps,
            codec=video.codec.upper(), kbps=info.total_kbps))
    lines.append(T.PLAN_TARGET.format(size=pl.human_size(plan.budget),
                                      margin=float(settings["safety_margin_pct"])))
    lines.append("")

    if plan.mode == "remux":
        lines.append(T.PLAN_MODE_REMUX)
    else:
        lines.append(T.PLAN_MODE_ENCODE.format(
            codec=CODEC_LABEL.get(plan.video.encoder, plan.video.encoder),
            encoder=plan.video.encoder, preset=plan.video.preset))
        scale = ""
        if plan.video.height and video is not None:
            scale = " · %dp ← %dp" % (plan.video.height, video.height)
        fps = " · %g fps" % plan.video.fps if plan.video.fps else ""
        if plan.guaranteed:
            lines.append(T.PLAN_ABR.format(kbps=plan.video.bitrate_kbps, scale=scale, fps=fps))
        else:
            lines.append(T.PLAN_QUALITY.format(crf=plan.video.quality, scale=scale, fps=fps))

    for audio in plan.audio:
        src = info.audio[audio.source]
        if audio.action == "copy":
            lines.append(T.PLAN_AUDIO_COPY.format(codec=src.codec.upper(),
                                                  kbps=src.bitrate_kbps))
        else:
            channels = " · סטריאו" if audio.channels == 2 and src.channels > 2 else ""
            lines.append(T.PLAN_AUDIO_ENC.format(kbps=audio.bitrate_kbps, channels=channels))
    if info.subtitles:
        lines.append(T.PLAN_SUBS.format(mode=SUB_LABEL.get(plan.subtitles, plan.subtitles)))

    lines.append("")
    pct = 100.0 * plan.estimated_bytes / max(1, plan.budget)
    lines.append(T.PLAN_ESTIMATE.format(size=pl.human_size(plan.estimated_bytes), pct=pct))
    if plan.estimated_seconds:
        lines.append(T.PLAN_TIME.format(time=pl.human_time(plan.estimated_seconds)))
    if plan.notes:
        lines.append("")
        lines.extend("• " + note for note in plan.notes)
    lines.append("")
    lines.append("פלט: " + plan.output)
    return "\n".join(lines)
