# -*- coding: utf-8 -*-
"""Measuring picture quality with VMAF instead of arguing about it.

Two different jobs live here, and the difference matters:

* :func:`compare_heights` encodes the *same* short windows at two resolutions
  under identical conditions and asks which one scores better.  A relative
  comparison like that is sound.
* :func:`measure_output` scores the finished file against its source.  That is
  the only honest way to state an absolute number: a short test encode always
  flatters itself (a fresh keyframe and no rate control history can be worth
  twenty VMAF points), so we never present a sample score as "expected
  quality".
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from . import ffmpeg as ff
from .probe import MediaInfo

StatusCb = Optional[Callable]          # called with progress or a height

# The default VMAF model assumes 1080p viewing; there is a separate one for 4K.
MODEL_4K = "vmaf_4k_v0.6.1"
MODEL_4K_MIN_HEIGHT = 1800


def model_for(height: int) -> str:
    return MODEL_4K if height >= MODEL_4K_MIN_HEIGHT else ""


@dataclass
class Score:
    mean: float
    low: float                          # the worst frame in the windows
    harmonic: float

    @property
    def combined(self) -> float:
        """One number for decisions: the average matters, the worst frames too."""
        return 0.7 * self.mean + 0.3 * self.low


def available(tools: ff.Toolset) -> bool:
    return tools.has_filter("libvmaf")


def _vmaf(tools: ff.Toolset, distorted: str, reference: str, ref_start: float,
          seconds: float, upscale_to: Optional[tuple], cancel=None,
          dist_start: float = 0.0, match_fps: float = 0.0,
          pair_fps: float = 0.0, model: str = "") -> Optional[Score]:
    """Score one window of ``distorted`` against the same window of the source.

    The two streams are re-stamped by frame *index* before they meet, so VMAF
    pairs frame 1 with frame 1 no matter what timebase each container used.
    Leaving the original timestamps in place looks harmless and is not: an MKV
    carries them in milliseconds while the source counts in 1/90000, and at 24
    fps the rounding drifts enough to mispair frames.  Measured on a visually
    lossless clip, that scored 73.9 instead of 99.96 - a quarter of the scale
    thrown away, silently, in the direction that looks like a real result.
    """
    folder = tempfile.mkdtemp(prefix="shrink2tg-vmaf-")
    log = os.path.join(folder, "vmaf.json")
    try:
        rate = pair_fps or match_fps or 25.0
        chain = "[0:v]"
        if upscale_to:
            chain += "scale=%d:%d:flags=lanczos," % upscale_to
        if match_fps:
            # A reduced frame rate must be restored first, or the two streams
            # would not even hold the same number of frames.
            chain += "fps=%g," % match_fps
        chain += "settb=AVTB,setpts=N/%g/TB[d];" % rate
        chain += "[1:v]settb=AVTB,setpts=N/%g/TB[r];" % rate
        chain += "[d][r]libvmaf=n_threads=8:"
        if model:
            # The default model is trained on 1080p viewing; using it on a 4K
            # pair reads several points high.  Only ask for the 4K model when
            # the build actually ships it, since a missing model is a hard error.
            chain += "model=version=%s:" % model
        chain += "log_fmt=json:log_path='%s'" % ff.escape_filter_path(log)
        args = [tools.ffmpeg, "-y"]
        if dist_start:
            args += ["-ss", "%.3f" % dist_start]
        args += ["-t", "%.3f" % seconds, "-i", distorted,
                 "-ss", "%.3f" % ref_start, "-t", "%.3f" % seconds, "-i", reference,
                 "-lavfi", chain, "-f", "null", "-"]
        res = ff.run_encode(args, cancel=cancel)
        if (not res.ok or not os.path.isfile(log)) and model:
            # This build does not ship the 4K model; the default one still
            # gives a usable number, so score with it rather than report none.
            return _vmaf(tools, distorted, reference, ref_start, seconds,
                         upscale_to, cancel, dist_start, match_fps, pair_fps,
                         model="")
        if not res.ok or not os.path.isfile(log):
            return None
        with open(log, "r", encoding="utf-8") as handle:
            pooled = json.load(handle).get("pooled_metrics", {}).get("vmaf", {})
        if "mean" not in pooled:
            return None
        return Score(mean=float(pooled.get("mean", 0.0)),
                     low=float(pooled.get("min", 0.0)),
                     harmonic=float(pooled.get("harmonic_mean", 0.0)))
    except (OSError, ValueError):
        return None
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def measure_output(tools: ff.Toolset, info: MediaInfo, output: str,
                   starts: Sequence[float], seconds: float = 6.0,
                   on_status: StatusCb = None, cancel=None,
                   upscale_to: Optional[tuple] = None,
                   match_fps: float = 0.0) -> Optional[Score]:
    """Score the delivered file against the source at a few points."""
    if not available(tools) or not os.path.isfile(output):
        return None
    scores: List[Score] = []
    for index, start in enumerate(starts):
        if cancel is not None and cancel.is_set():
            return None
        if on_status:
            on_status(index + 1, len(starts))
        # Both files share one timeline, so the same seek lands on the same frame.
        score = _vmaf(tools, output, info.path, start, seconds,
                      upscale_to=upscale_to, cancel=cancel, dist_start=start,
                      match_fps=match_fps,
                      pair_fps=info.video.fps if info.video else 0.0,
                      model=model_for(info.video.height if info.video else 0))
        if score:
            scores.append(score)
    if not scores:
        return None
    return Score(
        mean=sum(s.mean for s in scores) / len(scores),
        low=min(s.low for s in scores),
        harmonic=sum(s.harmonic for s in scores) / len(scores),
    )


def compare_heights(tools: ff.Toolset, info: MediaInfo, encode_sample,
                    heights: Sequence[int], starts: Sequence[float],
                    seconds: float = 6.0, on_status: StatusCb = None,
                    cancel=None) -> Optional[dict]:
    """Encode the same windows at each height and return {height: Score}.

    ``encode_sample(height, start, seconds, path)`` does the encoding, so the
    caller keeps full control over the encoder settings being tested.
    """
    if not available(tools) or not info.video:
        return None
    folder = tempfile.mkdtemp(prefix="shrink2tg-ab-")
    results: dict = {}
    try:
        for height in heights:
            if cancel is not None and cancel.is_set():
                return None
            if on_status:
                on_status(height)
            collected: List[Score] = []
            for index, start in enumerate(starts):
                path = os.path.join(folder, "ab_%d_%d.mkv" % (height, index))
                if not encode_sample(height, start, seconds, path):
                    return None
                upscale = None
                if height and height != info.video.height:
                    upscale = (info.video.width, info.video.height)
                score = _vmaf(tools, path, info.path, start, seconds,
                              upscale_to=upscale, cancel=cancel,
                              pair_fps=info.video.fps,
                              model=model_for(info.video.height))
                if score:
                    collected.append(score)
                try:
                    os.remove(path)
                except OSError:
                    pass
            if collected:
                results[height] = Score(
                    mean=sum(s.mean for s in collected) / len(collected),
                    low=min(s.low for s in collected),
                    harmonic=sum(s.harmonic for s in collected) / len(collected),
                )
        return results or None
    finally:
        shutil.rmtree(folder, ignore_errors=True)
