# -*- coding: utf-8 -*-
"""A cheap map of how demanding each part of the video is.

Reading a few short packet intervals with ffprobe costs well under a second even
on a 20GB file (no decoding happens - only the container index is touched), and
it tells us two things the planner used to guess at: where to take test samples
so they represent the whole film, and how far off a sample based estimate is.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import ffmpeg as ff
from .probe import MediaInfo

WINDOW_SECONDS = 2.0
WINDOW_COUNT = 40
GAP = 1.0                               # a jump this big means a new interval


@dataclass
class Profile:
    """Bitrate of the source at many points, in kbps."""
    windows: List[Tuple[float, float]] = field(default_factory=list)   # (start, kbps)

    @property
    def values(self) -> List[float]:
        return [kbps for _, kbps in self.windows]

    @property
    def mean(self) -> float:
        values = self.values
        return sum(values) / len(values) if values else 0.0

    def percentile(self, fraction: float) -> float:
        values = sorted(self.values)
        if not values:
            return 0.0
        index = min(len(values) - 1, max(0, int(round(fraction * (len(values) - 1)))))
        return values[index]

    @property
    def median(self) -> float:
        return self.percentile(0.5)

    @property
    def peak(self) -> float:
        return self.percentile(0.95)

    @property
    def spread(self) -> float:
        low = max(1.0, self.percentile(0.1))
        return self.peak / low


def _intervals(points: List[float], seconds: float) -> str:
    return ",".join("%.2f%%+%.2f" % (start, seconds) for start in points)


def _bitrates(tools: ff.Toolset, info: MediaInfo, points: List[float],
              seconds: float) -> List[Tuple[float, float]]:
    """Measure each requested interval from the packets ffprobe really returns.

    ffprobe starts at the keyframe before the requested time, so the window we
    get is never exactly the window we asked for - we therefore derive the
    bitrate from the timestamps of the packets themselves.
    """
    res = ff.run(
        [tools.ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=size,pts_time", "-of", "csv=p=0",
         "-read_intervals", _intervals(points, seconds), info.path],
        timeout=180,
    )
    if not res.ok:
        return []
    packets: List[Tuple[float, int]] = []
    for line in res.stdout.splitlines():
        stamp, _, size = line.partition(",")
        try:
            packets.append((float(stamp), int(size)))
        except ValueError:
            continue
    packets.sort()

    fps = (info.video.fps if info.video and info.video.fps else 30.0)
    groups: List[List[Tuple[float, int]]] = []
    current: List[Tuple[float, int]] = []
    for stamp, size in packets:
        if current and stamp - current[-1][0] > GAP:
            groups.append(current)
            current = []
        current.append((stamp, size))
    if current:
        groups.append(current)

    # Each group carries its own position, so nothing depends on the request
    # order and a window that produced no packets simply does not appear.
    out: List[Tuple[float, float]] = []
    for group in groups:
        span = (group[-1][0] - group[0][0]) + 1.0 / fps
        if span >= 0.2:
            out.append((group[0][0], sum(size for _, size in group) * 8 / span / 1000.0))
    return out


def measure(tools: ff.Toolset, info: MediaInfo, count: int = WINDOW_COUNT,
            seconds: float = WINDOW_SECONDS) -> Optional[Profile]:
    if info.duration <= seconds * 3:
        return None
    # Keep the windows clear of each other, or short files would just return one
    # long overlapping blob instead of a profile.
    count = max(4, min(count, int(info.duration / (seconds * 3))))
    points = [info.duration * (i + 0.5) / count for i in range(count)]
    windows = _bitrates(tools, info, points, seconds)
    if len(windows) < max(4, count // 3):
        return None                     # unreliable (odd container, no index)
    return Profile(windows=windows)


def representative_starts(profile: Profile, count: int, seconds: float,
                          duration: float) -> List[float]:
    """Pick sample positions that cover the whole range of complexity.

    Sorting the windows by how demanding they are and taking the middle of each
    equally sized band gives easy, average and hard content in one set, instead
    of whatever happens to sit at fixed positions in the timeline.
    """
    windows = sorted(profile.windows, key=lambda item: item[1])
    if not windows:
        return []
    count = max(1, count)
    picks: List[float] = []
    band = len(windows) / float(count)
    for i in range(count):
        index = min(len(windows) - 1, int(band * i + band / 2))
        start = windows[index][0]
        picks.append(max(0.0, min(start, duration - seconds)))
    return sorted(set(picks))


def local_bitrate(profile: Profile, start: float, seconds: float) -> float:
    """Complexity of the window nearest to ``start``."""
    if not profile.windows:
        return 0.0
    return min(profile.windows, key=lambda item: abs(item[0] - start))[1]


def correction_factor(profile: Profile, starts: List[float], seconds: float) -> float:
    """How much a measurement taken at ``starts`` has to be scaled.

    If the sampled windows are busier than the film as a whole, an encode of
    those windows costs more per second than the film will, and the factor pulls
    the estimate back down (and the other way around).
    """
    if not starts or not profile.windows:
        return 1.0
    sampled = [local_bitrate(profile, start, seconds) for start in starts]
    sampled_mean = sum(sampled) / len(sampled)
    if sampled_mean <= 0 or profile.mean <= 0:
        return 1.0
    factor = profile.mean / sampled_mean
    return max(0.5, min(2.0, factor))   # never trust it beyond a factor of two
