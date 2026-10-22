# -*- coding: utf-8 -*-
"""Turning a :class:`~shrink2tg.planner.Plan` into a finished MP4."""
from __future__ import annotations

import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import ffmpeg as ff
from . import planner as pl
from . import quality
from . import texts as T
from .probe import MediaInfo

ProgressCb = Optional[Callable[[float, float, float], None]]   # fraction, speed, eta
LogCb = Optional[Callable[[str], None]]
StatusCb = Optional[Callable[[str], None]]


@dataclass
class EncodeResult:
    ok: bool
    output: str = ""
    size: int = 0
    message: str = ""
    cancelled: bool = False


def build_command(tools: ff.Toolset, plan: pl.Plan, pass_no: int = 0,
                  passlog: str = "", output: str = "") -> List[str]:
    """Assemble the complete ffmpeg command line for one pass of the plan."""
    info, video = plan.info, plan.video
    first_pass = pass_no == 1
    args: List[str] = [tools.ffmpeg, "-y", "-i", info.path] + pl.video_map(info)

    if not first_pass:
        for audio in plan.audio:
            args += ["-map", "0:a:%d?" % audio.source]
        if plan.subtitles == "text":
            for i, sub in enumerate(info.subtitles):
                if sub.is_text:
                    args += ["-map", "0:s:%d?" % i]
    args += ["-dn"]

    if video.action == "copy":
        args += ["-c:v", "copy"]
        if info.video is not None and info.video.codec == "hevc":
            # Without this the stream lands as hev1, which Apple players and
            # several Telegram clients refuse to open.
            args += ["-tag:v", "hvc1"]
    else:
        burn = None
        if plan.subtitles == "burn" and plan.burn_index >= 0:
            burn = (info.path, plan.burn_index)
        args += pl.video_args(video, info, pass_no, burn)

    if first_pass:
        # Nothing is written in the analysis pass - only the statistics file.
        null_out = "NUL" if os.name == "nt" else "/dev/null"
        return args + ["-an", "-sn", "-pass", "1", "-passlogfile", passlog,
                       "-f", "null", null_out]
    if pass_no == 2:
        args += ["-pass", "2", "-passlogfile", passlog]

    for i, audio in enumerate(plan.audio):
        if audio.action == "copy":
            args += ["-c:a:%d" % i, "copy"]
        else:
            args += ["-c:a:%d" % i, "aac", "-b:a:%d" % i, "%dk" % audio.bitrate_kbps]
            if audio.channels:
                args += ["-ac:a:%d" % i, str(audio.channels)]
    if plan.subtitles == "text":
        args += ["-c:s", "mov_text"]

    args += ["-map_metadata", "0", "-map_chapters", "0",
             "-movflags", "+faststart", "-max_muxing_queue_size", "4096",
             output or plan.output]
    return args


def _discard(path: str) -> None:
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _progress_adapter(duration: float, base: float, span: float,
                      on_progress: ProgressCb) -> Callable[[ff.Progress], None]:
    def handler(progress: ff.Progress) -> None:
        if not on_progress or duration <= 0:
            return
        done = min(1.0, max(0.0, progress.seconds_done / duration))
        remaining = max(0.0, duration - progress.seconds_done)
        speed = progress.speed or 0.0
        eta = remaining / speed if speed > 0.05 else 0.0
        if span < 0.99 and speed > 0.05:
            eta += duration / speed * ((1.0 - base - span) / max(0.01, span))
        on_progress(base + done * span, speed, eta)
    return handler


def _run_once(tools: ff.Toolset, plan: pl.Plan, on_progress: ProgressCb,
              on_status: StatusCb, on_log: LogCb, cancel) -> EncodeResult:
    duration = plan.info.duration
    # Hardware encoders ignore the stats file, so a first pass would only
    # double the wall clock time for nothing.
    two_pass = (plan.video.action == "encode" and plan.video.bitrate_kbps > 0
                and plan.video.two_pass
                and plan.video.encoder in ("libx265", "libx264"))
    tmpdir = tempfile.mkdtemp(prefix="shrink2tg-pass-")
    passlog = os.path.join(tmpdir, "ffpass")
    try:
        if two_pass:
            if on_status:
                on_status(T.STATUS_ENCODING.format(name=os.path.basename(plan.info.path)))
            cmd = build_command(tools, plan, pass_no=1, passlog=passlog)
            res = ff.run_encode(cmd, _progress_adapter(duration, 0.0, 0.45, on_progress),
                                cancel, on_log)
            if res.returncode == -9:
                return EncodeResult(False, cancelled=True, message=T.STATUS_CANCELLED)
            if not res.ok:
                return EncodeResult(False, message=res.stderr)
            cmd = build_command(tools, plan, pass_no=2, passlog=passlog)
            res = ff.run_encode(cmd, _progress_adapter(duration, 0.45, 0.55, on_progress),
                                cancel, on_log)
        else:
            cmd = build_command(tools, plan)
            res = ff.run_encode(cmd, _progress_adapter(duration, 0.0, 1.0, on_progress),
                                cancel, on_log)
        if res.returncode == -9:
            return EncodeResult(False, cancelled=True, message=T.STATUS_CANCELLED)
        if not res.ok:
            return EncodeResult(False, message=res.stderr)
        if not os.path.isfile(plan.output):
            return EncodeResult(False, message=res.stderr or "no output produced")
        return EncodeResult(True, plan.output, os.path.getsize(plan.output))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _report_quality(tools: ff.Toolset, plan: pl.Plan, settings,
                    on_status: StatusCb, on_note: StatusCb, cancel) -> None:
    """Score the finished file against its source and say what we got.

    Only the delivered file can answer this honestly - a short test encode
    scores far above what a full length encode of the same content reaches.
    """
    info = plan.info
    if not bool(settings["quality_check"]) or plan.video.action == "copy":
        return
    if not quality.available(tools) or info.video is None:
        return
    if plan.estimated_seconds and plan.estimated_seconds < 180:
        return                              # a minute of measuring, for nothing
    starts = list(plan.sample_starts)[:3] or pl.sample_starts(info.duration, 3, 6.0)
    upscale = None
    if plan.video.height and plan.video.height != info.video.height:
        upscale = (info.video.width, info.video.height)
    score = quality.measure_output(
        tools, info, plan.output, starts, 6.0,
        on_status=lambda i, n: on_status and on_status(T.STATUS_QUALITY.format(i=i, n=n)),
        cancel=cancel, upscale_to=upscale,
        match_fps=(info.video.fps if plan.video.fps else 0.0),
    )
    if score and on_note:
        on_note(T.QUALITY_RESULT.format(mean=score.mean, low=score.low,
                                        windows=len(starts)))
        on_note(T.QUALITY_SCALE)


def run_plan(tools: ff.Toolset, plan: pl.Plan, settings,
             on_progress: ProgressCb = None, on_status: StatusCb = None,
             on_log: LogCb = None, cancel=None, on_note: StatusCb = None) -> EncodeResult:
    """Execute the plan, then make sure the result really fits the limit."""
    info = plan.info
    if not os.path.isfile(info.path):
        return EncodeResult(False, message=T.ERR_FILE_MISSING.format(path=info.path))
    folder = os.path.dirname(plan.output)
    if folder:
        os.makedirs(folder, exist_ok=True)

    name = os.path.basename(info.path)
    max_crf = int(settings["max_crf"])
    def note(text: str) -> None:
        plan.notes.append(text)
        if on_note:
            on_note(text)

    for attempt in range(3):
        if on_status:
            if attempt:
                on_status(T.STATUS_ENCODING_RETRY.format(attempt=attempt + 1, name=name))
            elif plan.mode == "remux":
                on_status(T.STATUS_REMUX.format(name=name))
            else:
                on_status(T.STATUS_ENCODING.format(name=name))
        result = _run_once(tools, plan, on_progress, on_status, on_log, cancel)
        if not result.ok:
            _discard(plan.output)                   # never leave half a file behind
            return result
        limit = plan.hard_limit or plan.budget
        if result.size <= limit or plan.video.action == "copy":
            _report_quality(tools, plan, settings, on_status, on_note, cancel)
            return result
        if attempt == 2:
            # Out of retries: the file is complete and playable but too big, so
            # say so instead of reporting success.
            return EncodeResult(
                False, plan.output, result.size,
                message=T.ERR_OVER_BUDGET.format(size=pl.human_size(result.size),
                                                 budget=pl.human_size(limit)))

        # The encoder overshot the budget: tighten the settings and try again.
        overshoot = result.size / float(plan.budget)
        _discard(plan.output)
        if plan.video.bitrate_kbps > 0:
            plan.video.bitrate_kbps = max(150, int(plan.video.bitrate_kbps / overshoot * 0.97))
            note(T.NOTE_RETRY_BITRATE.format(kbps=plan.video.bitrate_kbps))
        else:
            step = max(1, int(round(6.0 * math.log(overshoot, 2))))
            new_crf = min(max_crf, plan.video.quality + step)
            if new_crf <= plan.video.quality:
                audio = pl.audio_kbps(plan.audio, info)
                budget_kbps = plan.budget * 8 / 1000.0 / max(0.1, info.duration)
                plan.video.bitrate_kbps = max(150, int(
                    budget_kbps - pl.mux_overhead_kbps(info, plan.video.fps) - audio))
                note(T.PLAN_GUARANTEED)
            else:
                plan.video.quality = new_crf
                note(T.NOTE_RETRY.format(crf=new_crf))
    return EncodeResult(False, message=T.ERR_TOO_BIG)
