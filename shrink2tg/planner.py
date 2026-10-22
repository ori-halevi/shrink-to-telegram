# -*- coding: utf-8 -*-
"""Deciding *how* to shrink a given file - the brain of the application.

The planner never guesses blindly.  It inspects the source, computes the exact
bitrate budget the Telegram limit allows, and (when deep analysis is enabled)
runs short test encodes on samples taken from across the video, so the final
quality setting is derived from the real content instead of a rule of thumb.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from . import complexity
from . import ffmpeg as ff
from . import quality
from . import texts as T
from .probe import MediaInfo, MP4_AUDIO_COPY_OK

GB = 1_000_000_000                      # Telegram counts in decimal gigabytes
HEIGHT_LADDER = [2160, 1440, 1080, 900, 720, 576, 480]
# MP4 overhead is per packet, not a share of the file: every frame costs a few
# bytes of index whatever its size.  Measured at a flat 33.4 B per video frame
# (one video plus one audio track, faststart), which is 1.3% of a 400 kbps file
# but only 0.18% of a 4000 kbps one - so charging a flat percentage would hand
# back a chunk of the budget for nothing at exactly the bitrates that matter.
MUX_BYTES_PER_FRAME = 34.0
TWO_PASS_FACTOR = 1.45                  # the analysis pass is cheaper than the encode
SAMPLE_BIAS = 0.97                      # samples never cover the hardest scene
ABR_MARGIN = 0.97                       # single pass rate control lands a few % high
AUDIO_MIN_KBPS = 64                     # below this even speech starts to suffer
VBV_PEAK = 2.5                          # allowed peak, as a multiple of the average
AB_BPP_LOW, AB_BPP_HIGH = 0.015, 0.070  # where downscaling is genuinely arguable

# Bits per pixel per frame that HEVC needs for a comfortable picture.
BPP_KEEP = 0.030                        # above this there is no need to scale
BPP_GOOD = 0.038                        # what we aim for after downscaling
BPP_H264_FACTOR = 1.6                   # H.264 needs considerably more bits

# When only a shallow cut is needed, H.264 still delivers an excellent picture
# and plays on every Telegram client; HEVC's efficiency is only worth its
# compatibility risk once the budget really bites.
# Measured on a real 1080p24 job at ratio 0.63: H.264 scored VMAF 94.9 against
# 96.5 for HEVC - 1.6 points for 2.5x the time.  The bits-per-pixel guard is
# what actually protects the picture; this ratio only keeps H.264 away from
# cuts deep enough for generation loss to show.
H264_CUT_RATIO = 0.55                   # target bitrate / source bitrate

# ------------------------------------------------------- the quality dial
# One dial the user can drag instead of three expert settings.  Each stop is a
# complete recipe; the speeds are measured, not guessed (1080p24, this machine,
# see QualityLevel.fps_1080p).  "codec" says how the encoder is chosen:
#   hardware - a GPU encoder if one really works here, else the fast software one
#   h264     - force libx264, the one codec every Telegram client plays
#   auto     - decide H.264 vs H.265 by how deep the cut is (h264_cut_ratio)
#   hevc     - force libx265, paying time for efficiency
@dataclass
class QualityLevel:
    level: int
    name: str
    codec: str
    preset: str
    two_pass: bool
    fps_1080p: float            # measured frames per second at 1920x1080
    vmaf: float                 # measured on a real 1080p job at 3119 kbps
    note: str


QUALITY_LADDER: List[QualityLevel] = [
    QualityLevel(1, "מהיר מאוד", "hardware", "quality", False, 222.9, 95.8,
                 "קידוד בכרטיס המסך. הכי מהיר בהרבה, והאיכות טובה מאוד. "
                 "הפלט הוא H.265, שלא מתנגן באותה אמינות בכל לקוח של טלגרם."),
    QualityLevel(2, "מהיר", "h264", "medium", False, 87.6, 94.9,
                 "H.264 במעבר אחד. תאימות מלאה בכל מכשיר, זמן קצר."),
    QualityLevel(3, "מאוזן", "h264", "medium", True, 87.6, 95.0,
                 "מוסיף מעבר ניתוח: הגודל מדויק יותר והביטים מתחלקים טוב יותר לאורך הסרט."),
    QualityLevel(4, "איכות טובה", "h264", "slow", True, 66.1, 95.2,
                 "H.264 ב-preset איטי יותר. עדיין תאימות מלאה."),
    QualityLevel(5, "איכות גבוהה", "hevc", "fast", True, 47.8, 95.4,
                 "מעבר ל-H.265. יעיל יותר לכל ביט, משתלם כשהכיווץ עמוק."),
    QualityLevel(6, "איכות מרבית", "hevc", "medium", True, 34.9, 96.5,
                 "H.265 ב-preset בינוני. קפיצת האיכות האמיתית האחרונה של המד."),
    QualityLevel(7, "קיצוני", "hevc", "slow", True, 12.7, 97.0,
                 "השלב האיטי ביותר שמוצע. מעליו נמדד preset slower: "
                 "פי 4 זמן תמורת 0.3 נקודת VMAF, ולכן הוא לא במד בכוונה."),
]
DEFAULT_LEVEL = 4


def quality_level(level: int) -> QualityLevel:
    for item in QUALITY_LADDER:
        if item.level == int(level):
            return item
    return QUALITY_LADDER[DEFAULT_LEVEL - 1]


def level_seconds(item: QualityLevel, info: MediaInfo) -> float:
    """Roughly how long this stop will take on this file.

    Scaled from a measured 1080p figure by pixel count; the real plan replaces
    this with a measurement, but the dial has to say something before you drag.
    """
    video = info.video
    if video is None or info.duration <= 0:
        return 0.0
    frames = info.duration * (video.fps or 25.0)
    pixels = max(1, video.width * video.height)
    fps = item.fps_1080p * (1920.0 * 1080.0) / pixels
    seconds = frames / max(0.5, fps)
    return seconds * (TWO_PASS_FACTOR if item.two_pass else 1.0)


def recommend_level(info: MediaInfo, target_gb: float = 4.0,
                    margin_pct: float = 2.0,
                    shallow_ratio: float = H264_CUT_RATIO) -> Tuple[int, str]:
    """Suggest a stop on the dial for this particular file.

    The case for spending hours is that the cut is deep enough for the extra
    effort to show on screen.  A file that barely has to shrink gets an
    excellent picture from the cheap stops, so recommending more would be
    selling time the user will never see.  This is also where the old
    "codec by depth of cut" rule lives now: instead of silently switching
    encoders it moves the recommendation, which the user can see and override.
    """
    video = info.video
    if video is None or info.duration <= 0:
        return DEFAULT_LEVEL, ""
    budget = budget_bytes(target_gb, margin_pct)
    if info.size <= budget:
        return 2, T.REC_ALREADY_SMALL
    budget_kbps = budget * 8 / 1000.0 / info.duration
    target = budget_kbps - mux_overhead_kbps(info) - 160.0
    source = video.bitrate_kbps or 0.0
    ratio = (target / source) if source > 0 else 0.0
    if ratio <= 0:
        return DEFAULT_LEVEL, ""
    if ratio >= shallow_ratio:
        level, why = 3, T.REC_SHALLOW.format(pct=ratio * 100.0)
    elif ratio >= 0.30:
        level, why = 5, T.REC_MEDIUM.format(pct=ratio * 100.0)
    else:
        level, why = 6, T.REC_DEEP.format(pct=ratio * 100.0)
    # Never recommend a stop that would run for most of a day on this file.
    while level > 2 and level_seconds(quality_level(level), info) > 6 * 3600:
        level -= 1
    return level, why


PRESET_TO_NVENC = {
    "ultrafast": "p1", "superfast": "p1", "veryfast": "p2", "faster": "p3",
    "fast": "p4", "medium": "p5", "slow": "p6", "slower": "p7", "veryslow": "p7",
}
HEVC_ENCODERS = ("libx265", "hevc_nvenc", "hevc_qsv", "hevc_amf")


# --------------------------------------------------------------- data model
@dataclass
class VideoPlan:
    action: str = "encode"              # encode | copy
    encoder: str = "libx265"
    preset: str = "medium"
    quality: int = 22                   # CRF / CQ / global_quality
    height: int = 0                     # 0 = keep the source height
    fps: float = 0.0                    # 0 = keep the source frame rate
    pix_fmt: str = "yuv420p"
    bitrate_kbps: int = 0               # >0 = fixed bitrate instead of CRF
    maxrate_kbps: int = 0               # VBV peak cap, also used in CRF mode
    keyint: int = 0                     # keyframe spacing in frames
    two_pass: bool = False              # analysis pass before the encode pass
    tonemap: bool = False
    keep_color: bool = True


@dataclass
class AudioPlan:
    source: int = 0                     # index within MediaInfo.audio
    action: str = "encode"              # encode | copy
    codec: str = "aac"
    bitrate_kbps: int = 160
    channels: int = 0                   # 0 = keep the source layout


@dataclass
class Plan:
    info: MediaInfo
    output: str
    budget: int                         # what the planner aims for (with margin)
    hard_limit: int = 0                 # the real Telegram limit; 0 = use budget
    video: VideoPlan = field(default_factory=VideoPlan)
    audio: List[AudioPlan] = field(default_factory=list)
    subtitles: str = "text"             # text | burn | none
    burn_index: int = -1
    estimated_bytes: int = 0
    estimated_seconds: float = 0.0
    measured_video_kbps: float = 0.0
    sample_starts: List[float] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return "remux" if self.video.action == "copy" else "encode"

    @property
    def guaranteed(self) -> bool:
        return self.video.bitrate_kbps > 0


# -------------------------------------------------------------------- utils
def budget_bytes(target_gb: float, margin_pct: float) -> int:
    return int(target_gb * GB * (1.0 - max(0.0, margin_pct) / 100.0))


def human_size(num: float) -> str:
    """Decimal units, the same way Telegram states its 2GB / 4GB limits."""
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1000.0 or unit == "TB":
            if unit in ("GB", "TB"):
                return "%.2f %s" % (value, unit)
            return "%.0f %s" % (value, unit)
        value /= 1000.0
    return "%.0f B" % value


def human_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


def mux_overhead_kbps(info: MediaInfo, fps: float = 0.0) -> float:
    """What the MP4 container itself will cost, in kbps."""
    rate = fps or (info.video.fps if info.video else 0.0) or 30.0
    return MUX_BYTES_PER_FRAME * rate * 8 / 1000.0


def _even(value: int) -> int:
    return int(value) - (int(value) % 2)


def _is_hevc(encoder: str) -> bool:
    return encoder in HEVC_ENCODERS


def bits_per_pixel(info: MediaInfo, kbps: float, height: int = 0,
                   fps: float = 0.0) -> float:
    """Bits per pixel per frame at a given output geometry."""
    video = info.video
    if video is None:
        return 0.0
    out_h = height or video.height or 1080
    ratio = video.width / float(video.height) if video.height else 16 / 9.0
    out_w = _even(round(out_h * ratio))
    out_fps = fps or video.fps or 30.0
    return (kbps * 1000.0) / max(1.0, out_w * out_h * max(1.0, out_fps))


# ----------------------------------------------------------- stream choices
def _default_audio(info: MediaInfo) -> int:
    for i, stream in enumerate(info.audio):
        if stream.default:
            return i
    return 0


def plan_audio(info: MediaInfo, keep_all: bool, max_kbps: int,
               budget_kbps: float, share: float = 0.12) -> List[AudioPlan]:
    """Give the audio a share of the budget, never an open ended amount.

    The old rule flipped to 128 kbps below a fixed video bitrate and gave every
    extra track its own full allowance, so five tracks could quietly take a
    megabit away from the picture.  Now all tracks divide one pool.
    """
    plans: List[AudioPlan] = []
    if not info.audio:
        return plans
    chosen = list(range(len(info.audio))) if keep_all else [_default_audio(info)]
    pool = min(max(budget_kbps * share, AUDIO_MIN_KBPS), budget_kbps * 0.5)
    if keep_all and len(chosen) > 1:
        # When the pool cannot pay for every track at a decent rate, keeping
        # fewer tracks beats starving the picture for all of them.
        affordable = max(1, int(pool // AUDIO_MIN_KBPS))
        if affordable < len(chosen):
            default = _default_audio(info)
            ordered = [default] + [i for i in chosen if i != default]
            chosen = sorted(ordered[:affordable])
    per_track = int(max(AUDIO_MIN_KBPS, min(float(max_kbps), pool / len(chosen))))
    for idx in chosen:
        src = info.audio[idx]
        surround = src.channels > 2 and per_track >= 224
        target = per_track
        can_copy = (
            src.codec in MP4_AUDIO_COPY_OK
            and 0 < src.bitrate_kbps <= target * 1.35
        )
        if can_copy:
            plans.append(AudioPlan(source=idx, action="copy", codec=src.codec,
                                   bitrate_kbps=int(src.bitrate_kbps)))
        else:
            plans.append(AudioPlan(source=idx, action="encode", codec="aac",
                                   bitrate_kbps=target,
                                   channels=0 if surround else (2 if src.channels > 2 else 0)))
    return plans


def audio_kbps(plans: List[AudioPlan], info: MediaInfo) -> float:
    total = 0.0
    for plan in plans:
        if plan.action == "copy":
            total += info.audio[plan.source].bitrate_kbps or 192.0
        else:
            total += plan.bitrate_kbps
    return total


HARDWARE_ORDER = ("hevc_nvenc", "hevc_qsv", "hevc_amf")


def pick_hardware(tools: ff.Toolset) -> str:
    """The best GPU encoder that is not merely compiled in but actually works."""
    for name in HARDWARE_ORDER:
        if tools.has(name) and ff.probe_hardware(tools, name):
            return name
    return ""


def pick_encoder(tools: ff.Toolset, requested: str, cut_ratio: float = 0.0,
                 source_codec: str = "", threshold: float = H264_CUT_RATIO,
                 bias: str = "auto", hdr_source: bool = False
                 ) -> Tuple[str, List[str]]:
    """Choose the encoder, trading HEVC's efficiency against H.264's reach.

    ``cut_ratio`` is how much of the source bitrate the budget still allows.
    Close to 1.0 the file barely has to shrink, H.264 will look excellent at
    that rate, and it is the one codec every Telegram client plays - so there
    is nothing to buy with HEVC's patchier playback support.  Pass 0.0 to say
    "H.264 is not an option here" (unknown source bitrate, or it would force a
    downscale that HEVC avoids).
    """
    notes: List[str] = []
    if requested and requested != "auto":
        if tools.has(requested):
            # Being compiled in says nothing about the card actually being here.
            if not requested.endswith(("nvenc", "qsv", "amf")) or ff.probe_hardware(tools, requested):
                return requested, notes
            notes.append(T.NOTE_HW_UNAVAILABLE.format(encoder=requested))
        else:
            notes.append(T.NOTE_ENCODER_FALLBACK)
    if bias == "hardware":
        gpu = pick_hardware(tools)
        if gpu and not (hdr_source and not _is_hevc(gpu)):
            return gpu, notes
        notes.append(T.NOTE_NO_HARDWARE)
        if tools.has("libx264"):
            return "libx264", notes
    elif bias == "h264" and tools.has("libx264"):
        # The dial asks for H.264, but H.264 cannot carry this picture: the
        # tool would hand back an 8 bit SDR copy of an HDR master.  Keeping the
        # speed the user asked for is not worth destroying the source, so the
        # codec is overridden and the log says why.
        if hdr_source and tools.has("libx265"):
            notes.append(T.NOTE_HDR_FORCES_HEVC)
            return "libx265", notes
        return "libx264", notes
    elif bias == "hevc" and tools.has("libx265"):
        return "libx265", notes
    # Re-encoding an HEVC source as H.264 would throw away efficiency to buy
    # compatibility the source never had, so that case stays on HEVC.
    if (threshold > 0 and cut_ratio >= threshold and source_codec != "hevc"
            and tools.has("libx264")):
        notes.append(T.NOTE_H264_SHALLOW.format(pct=cut_ratio * 100.0))
        return "libx264", notes
    if tools.has("libx265"):
        return "libx265", notes
    if tools.has("libx264"):
        return "libx264", notes
    raise RuntimeError("No usable video encoder in this ffmpeg build")


def pick_pix_fmt(info: MediaInfo, encoder: str) -> str:
    video = info.video
    if video is None:
        return "yuv420p"
    if _is_hevc(encoder) and video.bit_depth >= 10:
        return "p010le" if encoder != "libx265" else "yuv420p10le"
    return "yuv420p"


def quality_flags(video: VideoPlan, pass_no: int = 0) -> List[str]:
    """Translate the abstract quality number into encoder specific flags."""
    encoder, quality, preset = video.encoder, video.quality, video.preset
    if video.bitrate_kbps > 0:                             # fixed bitrate
        rate = video.bitrate_kbps
        if encoder.endswith("nvenc"):
            return ["-preset", PRESET_TO_NVENC.get(preset, "p5"), "-rc", "vbr",
                    "-b:v", "%dk" % rate, "-maxrate", "%dk" % int(rate * 1.5),
                    "-bufsize", "%dk" % (rate * 3)]
        if encoder.endswith("qsv"):
            return ["-preset", preset, "-b:v", "%dk" % rate,
                    "-maxrate", "%dk" % int(rate * 1.5)]
        if encoder.endswith("amf"):
            return ["-quality", "quality", "-rc", "vbr_peak", "-b:v", "%dk" % rate,
                    "-maxrate", "%dk" % int(rate * 1.5)]
        peak = video.maxrate_kbps or int(rate * VBV_PEAK)
        flags = ["-preset", preset, "-b:v", "%dk" % rate,
                 "-maxrate", "%dk" % peak, "-bufsize", "%dk" % (peak * 2)]
        if encoder == "libx265":
            # ffmpeg drives the x265 two pass machinery through -pass itself.
            flags += ["-x265-params", "log-level=error"]
        return flags
    cap: List[str] = []
    if video.maxrate_kbps > 0:
        # A peak cap keeps live playback in Telegram from stalling on the
        # hardest scenes; the head room is generous enough not to cost quality.
        cap = ["-maxrate", "%dk" % video.maxrate_kbps,
               "-bufsize", "%dk" % (video.maxrate_kbps * 2)]
    if encoder == "libx265":
        return ["-preset", preset, "-crf", str(quality),
                "-x265-params", "log-level=error"] + cap
    if encoder == "libx264":
        return ["-preset", preset, "-crf", str(quality)] + cap
    if encoder.endswith("nvenc"):
        return ["-preset", PRESET_TO_NVENC.get(preset, "p5"), "-tune", "hq",
                "-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
    if encoder.endswith("qsv"):
        return ["-preset", preset, "-global_quality", str(quality)]
    if encoder.endswith("amf"):
        return ["-quality", "quality", "-rc", "cqp",
                "-qp_i", str(quality), "-qp_p", str(quality), "-qp_b", str(quality)]
    return ["-crf", str(quality)]


def video_filters(video: VideoPlan, info: MediaInfo,
                  burn: Optional[Tuple[str, int]] = None,
                  burn_offset: float = 0.0) -> str:
    parts: List[str] = []
    if video.tonemap:
        parts.append("zscale=t=linear:npl=100,tonemap=tonemap=hable:desat=0,"
                     "zscale=p=bt709:t=bt709:m=bt709:r=tv")
    src = info.video
    if src is not None and not src.square_pixels:
        # Non square pixels: scale to the real display geometry and mark the
        # result square, because some Telegram players ignore the SAR tag.
        height = video.height or src.height
        width = _even(round(height * src.display_aspect))
        parts.append("scale=%d:%d:flags=lanczos,setsar=1" % (width, height))
    elif video.height:
        parts.append("scale=-2:%d:flags=lanczos" % video.height)
    if burn is not None:
        # Burning after the scaler keeps the letters crisp at the output size.
        # A sample is cut with an input seek, which rewinds the timestamps to
        # zero - the subtitles filter would then match the wrong cues, or far
        # more often none at all, and the sample would silently measure a
        # picture with no subtitles in it.  Pushing the frames back onto the
        # real timeline for the length of the filter fixes that.
        if burn_offset > 0:
            parts.append("setpts=PTS+%.3f/TB" % burn_offset)
        parts.append("subtitles='%s':si=%d" % (ff.escape_filter_path(burn[0]), burn[1]))
        if burn_offset > 0:
            parts.append("setpts=PTS-STARTPTS")
    if video.fps:
        parts.append("fps=%g" % video.fps)
    if video.pix_fmt:
        parts.append("format=%s" % video.pix_fmt)
    return ",".join(parts)


def video_map(info: MediaInfo) -> List[str]:
    """Map the actual movie stream - "0:v:0" could be an attached cover image."""
    if info.video is not None:
        return ["-map", "0:%d" % info.video.index]
    return ["-map", "0:v:0"]


def video_args(video: VideoPlan, info: MediaInfo, pass_no: int = 0,
               burn: Optional[Tuple[str, int]] = None,
               burn_offset: float = 0.0) -> List[str]:
    args = ["-c:v", video.encoder] + quality_flags(video, pass_no)
    filters = video_filters(video, info, burn, burn_offset)
    if filters:
        args += ["-vf", filters]
    src = info.video
    if src is not None and video.keep_color and not video.tonemap:
        if src.color_primaries:
            args += ["-color_primaries", src.color_primaries]
        if src.color_transfer:
            args += ["-color_trc", src.color_transfer]
        if src.color_space:
            args += ["-colorspace", src.color_space]
    if video.keyint:
        args += ["-g", str(video.keyint)]
    if _is_hevc(video.encoder):
        args += ["-tag:v", "hvc1"]          # required by most players/Telegram
    return args


def choose_scale(info: MediaInfo, target_video_kbps: float, allow_downscale: bool,
                 min_height: int, allow_fps_drop: bool, encoder: str,
                 prefer: str = "sharpness") -> Tuple[int, float, List[str]]:
    """Pick output height / fps so the available bitrate still looks good."""
    notes: List[str] = []
    video = info.video
    assert video is not None
    src_h = video.height or 1080
    src_w = video.width or 1920
    src_fps = video.fps or 30.0
    factor = 1.0 if _is_hevc(encoder) else BPP_H264_FACTOR
    need, aim = BPP_KEEP * factor, BPP_GOOD * factor

    def bpp(height: int, fps: float) -> float:
        return bits_per_pixel(info, target_video_kbps, height, fps)

    if bpp(src_h, src_fps) >= need:
        return 0, 0.0, notes

    out_fps, out_h = 0.0, 0
    cur_fps, cur_h = src_fps, src_h

    def drop_fps() -> bool:
        nonlocal cur_fps, out_fps
        if not allow_fps_drop or cur_fps <= 50:
            return False
        cur_fps = round(src_fps / 2.0, 3)
        out_fps = cur_fps
        notes.append(T.NOTE_FPS_DROP.format(src=src_fps, dst=cur_fps))
        return True

    def drop_size() -> bool:
        nonlocal cur_h, out_h
        if not allow_downscale:
            return False
        best = cur_h
        for candidate in HEIGHT_LADDER:
            if candidate >= cur_h or candidate < min_height:
                continue
            best = candidate
            if bpp(candidate, cur_fps) >= aim:
                break
        if best < cur_h:
            notes.append(T.NOTE_DOWNSCALE.format(src=cur_h, dst=best))
            cur_h = out_h = best
            return True
        return False

    # "sharpness" keeps every pixel and pays with motion; "smoothness" is the
    # other way round.  Whichever comes first, the second one is the fallback.
    order = (drop_fps, drop_size) if prefer == "sharpness" else (drop_size, drop_fps)
    for step in order:
        if bpp(cur_h, cur_fps) >= need:
            break
        step()
    if bpp(cur_h, cur_fps) < need and out_h == 0 and src_h <= min_height:
        # Already at (or below) the floor the user set: the bitrate has to give.
        notes.append(T.NOTE_MIN_HEIGHT_ABOVE_SOURCE.format(src=src_h, min=min_height))
    return out_h, out_fps, notes


# --------------------------------------------------------------- sampling
def sample_starts(duration: float, count: int, seconds: float) -> List[float]:
    first = duration * 0.05
    last = duration * 0.95 - seconds
    if last <= first or count <= 1:
        return [max(0.0, (duration - seconds) / 2.0)]
    step = (last - first) / float(count - 1)
    return [first + step * i for i in range(count)]


class SampleFailed(RuntimeError):
    """ffmpeg could not encode a test sample (bad encoder, unreadable file...)."""


@dataclass
class Measurement:
    kbps: float
    encode_speed: float                 # video seconds encoded per wall second


def measure_quality(tools: ff.Toolset, info: MediaInfo, video: VideoPlan,
                    starts: List[float], seconds: float,
                    on_sample: Optional[Callable[[int, int], None]] = None,
                    cancel=None,
                    burn: Optional[Tuple[str, int]] = None) -> Optional[Measurement]:
    """Encode a few short samples and report the bitrate they produce."""
    tmpdir = tempfile.mkdtemp(prefix="shrink2tg-")
    total_bytes = 0
    total_video_secs = 0.0
    started = time.time()
    try:
        for i, start in enumerate(starts):
            if cancel is not None and cancel.is_set():
                return None
            if on_sample:
                on_sample(i + 1, len(starts))
            out = os.path.join(tmpdir, "sample%d.mkv" % i)
            args = [tools.ffmpeg, "-y", "-ss", "%.3f" % start, "-i", info.path,
                    "-t", "%.3f" % seconds] + video_map(info) + ["-an", "-sn", "-dn"]
            args += video_args(video, info, burn=burn, burn_offset=start)
            args += ["-f", "matroska", out]
            res = ff.run_encode(args, cancel=cancel)
            if cancel is not None and cancel.is_set():
                return None
            if not res.ok or not os.path.isfile(out):
                raise SampleFailed((res.stderr or "").strip()[-600:] or "sample encode failed")
            total_bytes += os.path.getsize(out)
            total_video_secs += seconds
        if total_video_secs <= 0:
            return None
        elapsed = max(0.001, time.time() - started)
        return Measurement(
            kbps=total_bytes * 8 / 1000.0 / total_video_secs,
            encode_speed=total_video_secs / elapsed,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ------------------------------------------------------------ plan building
class Cancelled(Exception):
    pass


def output_path(info: MediaInfo, out_dir: str, suffix: str) -> str:
    folder = out_dir or os.path.dirname(info.path)
    base = os.path.splitext(os.path.basename(info.path))[0]
    candidate = os.path.join(folder, base + suffix + ".mp4")
    index = 2
    while os.path.exists(candidate) or os.path.abspath(candidate) == os.path.abspath(info.path):
        candidate = os.path.join(folder, "%s%s-%d.mp4" % (base, suffix, index))
        index += 1
    return candidate


def _subtitle_plan(info: MediaInfo, mode: str, notes: List[str]) -> Tuple[str, int]:
    if mode == "none" or not info.subtitles:
        return "none", -1
    text_subs = [i for i, sub in enumerate(info.subtitles) if sub.is_text]
    if mode == "burn":
        if text_subs:
            return "burn", text_subs[0]
        notes.append(T.NOTE_BITMAP_SUBS)
        return "none", -1
    if not text_subs:
        notes.append(T.NOTE_BITMAP_SUBS)
        return "none", -1
    notes.append(T.NOTE_SUBS_TELEGRAM)
    return "text", -1


def _remux_estimate(info: MediaInfo, plan: Plan) -> int:
    video_kbps = info.video.bitrate_kbps if info.video else 0.0
    total = video_kbps + audio_kbps(plan.audio, info)
    if total <= 0:
        return info.size
    total += mux_overhead_kbps(info)
    return int(min(info.size, total * 1000 / 8.0 * info.duration))


def _finish_estimate(plan: Plan, info: MediaInfo, video_kbps: float) -> None:
    total = (video_kbps + audio_kbps(plan.audio, info)
             + mux_overhead_kbps(info, plan.video.fps))
    plan.estimated_bytes = int(total * 1000 / 8.0 * info.duration)
    if not plan.estimated_seconds:
        pixels = (info.video.width * info.video.height) if info.video else 1920 * 1080
        speed = 1.6 if plan.video.encoder != "libx265" else 0.55
        speed *= (1920.0 * 1080.0) / max(1.0, pixels)
        plan.estimated_seconds = info.duration / max(0.05, speed)
    if plan.video.two_pass:
        plan.estimated_seconds *= TWO_PASS_FACTOR


def build_plan(tools: ff.Toolset, info: MediaInfo, settings,
               on_status: Optional[Callable[[str], None]] = None,
               cancel=None) -> Plan:
    """Work out the best way to bring ``info`` under the configured limit."""
    if info.video is None:
        raise ValueError(T.ERR_NO_VIDEO)
    if info.duration <= 0:
        raise ValueError(T.ERR_NO_DURATION)

    def status(text: str) -> None:
        if on_status:
            on_status(text)

    budget = budget_bytes(float(settings["target_gb"]), float(settings["safety_margin_pct"]))
    plan = Plan(
        info=info,
        output=output_path(info, settings["output_dir"], settings["output_suffix"]),
        budget=budget,
        # A finished file that is under the real limit is fine even if it missed
        # the internal budget - re-encoding it would cost hours for nothing.
        hard_limit=int(float(settings["target_gb"]) * GB),
    )

    budget_kbps = budget * 8 / 1000.0 / info.duration
    plan.audio = plan_audio(info, bool(settings["keep_all_audio"]),
                            int(settings["audio_max_kbps"]), budget_kbps,
                            float(settings["audio_budget_share"]))
    chosen_audio = audio_kbps(plan.audio, info)
    if plan.audio and plan.audio[0].action == "encode" and plan.audio[0].bitrate_kbps < int(settings["audio_max_kbps"]):
        plan.notes.append(T.NOTE_AUDIO_SHARE.format(kbps=plan.audio[0].bitrate_kbps))
    target_video_kbps = budget_kbps - mux_overhead_kbps(info) - chosen_audio
    if target_video_kbps < 100:
        plan.notes.append(T.NOTE_BUDGET_TOO_SMALL)
        target_video_kbps = 100.0
    plan.subtitles, plan.burn_index = _subtitle_plan(info, settings["subtitles"], plan.notes)

    if info.size <= budget and not info.video.copy_safe:
        if info.video.codec == "av1":
            plan.notes.append(T.NOTE_AV1_SOURCE)
        else:
            plan.notes.append(T.NOTE_COPY_UNSAFE.format(
                codec=info.video.codec.upper(), pix_fmt=info.video.pix_fmt or "?"))
    if info.size <= budget and info.video.copy_safe and plan.subtitles != "burn":
        plan.video = VideoPlan(action="copy")
        plan.estimated_bytes = _remux_estimate(info, plan)
        plan.estimated_seconds = max(5.0, info.size / (150 * 1024 * 1024.0))
        plan.notes.insert(0, T.NOTE_ALREADY_SMALL)
        if info.ext == ".mp4":
            plan.notes.insert(0, T.PLAN_NO_CHANGE)
        return plan

    # How shallow is the cut?  Only a source bitrate we actually know counts,
    # and H.264 is off the table when it would cost a downscale HEVC avoids.
    source_kbps = info.video.bitrate_kbps
    cut_ratio = (target_video_kbps / source_kbps) if source_kbps > 0 else 0.0
    if bits_per_pixel(info, target_video_kbps) < BPP_KEEP * BPP_H264_FACTOR:
        cut_ratio = 0.0
    dial = quality_level(settings["quality_level"])
    encoder, enc_notes = pick_encoder(
        tools, settings["encoder"], cut_ratio=min(cut_ratio, 1.0),
        source_codec=info.video.codec,
        threshold=float(settings["h264_cut_ratio"]),
        bias=dial.codec, hdr_source=info.video.is_hdr,
    )
    plan.notes.extend(enc_notes)
    plan.notes.append(T.NOTE_QUALITY_LEVEL.format(level=dial.level, name=dial.name))
    video = VideoPlan(
        action="encode",
        encoder=encoder,
        preset=dial.preset if not encoder.endswith(("nvenc", "qsv", "amf"))
        else settings["preset"],
        pix_fmt=pick_pix_fmt(info, encoder),
        tonemap=bool(settings["tonemap_hdr"]) and info.video.is_hdr,
    )
    plan.video = video
    if info.video.is_hdr:
        plan.notes.append(T.NOTE_TONEMAP if video.tonemap else T.NOTE_HDR)
    if info.video.codec == "hevc":
        plan.notes.append(T.NOTE_HEVC_SOURCE)

    min_crf = int(settings["min_crf"])
    if info.size <= budget:
        # Container or burned in subtitles force the encode; the size itself is
        # fine, but the result still has to be measured against the budget so a
        # re-encode can never come out larger than what we started with.
        plan.notes.append(T.NOTE_FORCED_REENCODE)
        target_video_kbps = min(target_video_kbps,
                                info.video.bitrate_kbps or target_video_kbps)

    height, fps, scale_notes = choose_scale(
        info, target_video_kbps, bool(settings["allow_downscale"]),
        int(settings["min_height"]), bool(settings["allow_fps_drop"]), encoder,
        str(settings["motion_priority"]),
    )
    video.height, video.fps = height, fps
    plan.notes.extend(scale_notes)

    gop_seconds = float(settings["keyframe_seconds"])
    if gop_seconds > 0:
        out_fps = video.fps or info.video.fps or 30.0
        video.keyint = max(12, int(round(out_fps * gop_seconds)))
        plan.notes.append(T.NOTE_KEYFRAME.format(seconds=gop_seconds))

    sample_burn = ((info.path, plan.burn_index)
                   if plan.subtitles == "burn" and plan.burn_index >= 0 else None)

    def sample_encoder(height: int, start: float, seconds: float, path: str) -> bool:
        """Encode one test window with the real settings, at a given height."""
        variant = VideoPlan(**vars(video))
        variant.height = 0 if height == info.video.height else height
        args = [tools.ffmpeg, "-y", "-ss", "%.3f" % start, "-i", info.path,
                "-t", "%.3f" % seconds] + video_map(info) + ["-an", "-sn", "-dn"]
        args += video_args(variant, info, burn=sample_burn, burn_offset=start)
        args += ["-f", "matroska", path]
        res = ff.run_encode(args, cancel=cancel)
        return bool(res.ok and os.path.isfile(path))

    def maybe_test_resolution() -> None:
        """Let a measurement settle the downscale question when it is arguable."""
        if not bool(settings["resolution_ab_test"]) or not quality.available(tools):
            return
        if not video.bitrate_kbps or not info.video.height:
            return
        current = video.height or info.video.height
        floor = int(settings["min_height"])
        candidates = [current]
        for candidate in HEIGHT_LADDER:         # one step down
            if candidate < current and candidate >= floor:
                candidates.append(candidate)
                break
        for candidate in reversed(HEIGHT_LADDER):   # and one step up, if any
            if current < candidate <= info.video.height:
                candidates.append(candidate)
                break
        if len(candidates) < 2:
            return
        out_fps = video.fps or info.video.fps or 30.0
        width = _even(round(current * info.video.display_aspect))
        density = video.bitrate_kbps * 1000.0 / max(1.0, width * current * out_fps)
        if not (AB_BPP_LOW <= density <= AB_BPP_HIGH):
            return                      # far from the fence: no need to measure
        scores = quality.compare_heights(
            tools, info, sample_encoder, candidates,
            (plan.sample_starts or [info.duration / 2])[:2], 6.0,
            on_status=lambda h: status(T.STATUS_RES_TEST.format(height=h)),
            cancel=cancel,
        )
        if not scores or len(scores) < 2:
            return
        best = max(scores.items(), key=lambda item: item[1].combined)
        if best[0] != current:
            # The measurement overruled the rule of thumb: drop its note so the
            # report does not claim two different resolutions.
            for stale in scale_notes:
                if stale in plan.notes:
                    plan.notes.remove(stale)
            if best[0] < info.video.height:
                plan.notes.append(T.NOTE_DOWNSCALE.format(
                    src=info.video.height, dst=best[0]))
            reference = (current, scores[current])
        else:
            reference = min(scores.items(), key=lambda item: item[1].combined)
        video.height = 0 if best[0] == info.video.height else best[0]
        plan.notes.append(T.NOTE_RES_AB.format(
            winner=best[0], loser=reference[0],
            win_score=best[1].combined, lose_score=reference[1].combined))

    def set_peak(average_kbps: float) -> None:
        """Cap the peaks so live playback survives, without costing quality.

        Never tighter than the bitrate the budget would have allowed anyway -
        spending that much on a hard scene is by definition affordable.
        """
        peak = max(average_kbps * VBV_PEAK, target_video_kbps, 200.0)
        video.maxrate_kbps = int(peak)
        plan.notes.append(T.NOTE_VBV.format(maxrate=video.maxrate_kbps))

    def use_fixed_bitrate() -> Plan:
        """Fill the budget exactly instead of guessing a CRF that misses it."""
        # Two pass rate control is accurate; single pass needs a little room.
        video.two_pass = dial.two_pass
        room = 1.0 if video.two_pass else ABR_MARGIN
        video.bitrate_kbps = max(200, int(target_video_kbps * room))
        if info.video.bitrate_kbps and video.bitrate_kbps > info.video.bitrate_kbps:
            video.bitrate_kbps = int(info.video.bitrate_kbps)
            plan.notes.append(T.NOTE_SOURCE_BITRATE_CAP)
        set_peak(video.bitrate_kbps)
        maybe_test_resolution()
        _finish_estimate(plan, info, video.bitrate_kbps)
        plan.notes.append(T.NOTE_FILL_BUDGET.format(crf=min_crf, kbps=video.bitrate_kbps))
        if video.two_pass:
            plan.notes.append(T.PLAN_TWO_PASS)
        return plan

    if not bool(settings["deep_analysis"]):
        source_kbps = info.video.bitrate_kbps
        if source_kbps and source_kbps <= target_video_kbps:
            # Even untouched the video is thinner than the budget allows.
            video.quality = min_crf
            set_peak(source_kbps)
            _finish_estimate(plan, info, source_kbps)
            plan.notes.append(T.NOTE_HEADROOM.format(crf=min_crf))
            return plan
        return use_fixed_bitrate()

    # Deep analysis. First a near free scan of the source structure, so that the
    # test samples cover easy *and* hard content, and so we know how far the
    # sampled windows sit from the film as a whole.
    seconds = float(settings["sample_seconds"])
    count = int(settings["sample_count"])
    profile = None
    if bool(settings["use_profile"]):
        status(T.STATUS_PROFILING)
        profile = complexity.measure(tools, info)
    if profile:
        plan.notes.append(T.NOTE_PROFILE.format(
            mean=profile.mean, median=profile.median,
            peak=profile.peak, spread=profile.spread))
        starts = complexity.representative_starts(profile, count, seconds, info.duration)
        correction = complexity.correction_factor(profile, starts, seconds)
    else:
        starts = sample_starts(info.duration, count, seconds)
        correction = 1.0
    plan.sample_starts = list(starts)

    # One question decides everything: does the *best* quality we are willing to
    # use already fit?  If it does, take it and stay small.  If it does not, the
    # budget is the binding constraint, and a fixed bitrate uses all of it while
    # a guessed CRF would land somewhere below it.
    probe = VideoPlan(**vars(video))
    probe.quality = min_crf
    probe.bitrate_kbps = 0
    probe.maxrate_kbps = 0
    measurement = measure_quality(
        tools, info, probe, starts, seconds,
        on_sample=lambda i, n: status(T.STATUS_SAMPLING.format(i=i, n=n, crf=min_crf)),
        cancel=cancel, burn=sample_burn,
    )
    if measurement is None:
        raise Cancelled()
    predicted_kbps = measurement.kbps * correction
    plan.measured_video_kbps = predicted_kbps
    plan.estimated_seconds = info.duration / max(0.01, measurement.encode_speed)
    plan.notes.append(T.NOTE_SAMPLE_RESULT.format(
        crf=min_crf, kbps=predicted_kbps,
        size=human_size(predicted_kbps * 1000 / 8.0 * info.duration)))
    if abs(correction - 1.0) > 0.05:
        plan.notes.append(T.NOTE_PROFILE_FIX.format(
            direction=T.DIR_HEAVIER if correction < 1.0 else T.DIR_LIGHTER,
            factor=correction))

    def climb_back(predicted_kbps: float) -> float:
        """Buy back what the rule of thumb gave away, if the budget allows.

        ``choose_scale`` decides the output size from a bits-per-pixel rule
        *before* anything is measured, so when the measurement then says the
        best quality we are willing to use fits comfortably, the picture was
        shrunk for nothing - and the leftover budget is quality thrown away.

        At a constant CRF a bigger picture is simply more picture, so this is a
        question of fit, not of taste: measure the larger variant and keep it if
        it still lands inside the budget.  Each step is paid for with one short
        sampling round, so the climb is capped.
        """
        climb_notes: List[str] = []
        ceiling = target_video_kbps * SAMPLE_BIAS
        if predicted_kbps > ceiling:
            return predicted_kbps
        src_h = info.video.height
        steps: List[Tuple[str, float]] = []
        if video.height:
            bigger = sorted(h for h in HEIGHT_LADDER if video.height < h <= src_h)
            steps.extend(("size", float(h)) for h in bigger)
            if src_h > video.height and src_h not in HEIGHT_LADDER:
                steps.append(("size", float(src_h)))
        if video.fps:
            steps.append(("fps", 0.0))
        if not steps:
            return predicted_kbps
        # choose_scale sacrifices in the order the user asked for, so buying
        # back runs the other way: whatever was given up last is the cheapest
        # to live without, and whatever was given up first is wanted most.
        if str(settings["motion_priority"]) != "sharpness":
            steps.reverse()

        current = predicted_kbps
        probe_seconds = min(6.0, seconds)
        probe_starts = list(starts)[:3] or starts
        for kind, value in steps[:3]:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            variant = VideoPlan(**vars(video))
            variant.quality = min_crf
            variant.bitrate_kbps = 0
            variant.maxrate_kbps = 0
            if kind == "size":
                variant.height = 0 if int(value) == src_h else int(value)
                label = "%dp" % int(value)
            else:
                variant.fps = 0.0
                label = "%g fps" % (info.video.fps or 0.0)
            status(T.STATUS_CLIMB.format(what=label))
            measured = measure_quality(
                tools, info, variant, probe_starts, probe_seconds,
                cancel=cancel, burn=sample_burn)
            if measured is None:
                raise Cancelled()
            candidate = measured.kbps * correction
            if candidate > ceiling:
                climb_notes.append(T.NOTE_CLIMB_STOP.format(
                    what=label, kbps=candidate))
                break
            video.height, video.fps = variant.height, variant.fps
            current = candidate
            climb_notes.append(T.NOTE_CLIMB.format(what=label, kbps=candidate))
        if climb_notes:
            # The downscale/fps notes described a decision that no longer holds.
            for stale in scale_notes:
                if stale in plan.notes:
                    plan.notes.remove(stale)
            if video.height:
                plan.notes.append(T.NOTE_DOWNSCALE.format(src=src_h, dst=video.height))
            if video.fps:
                plan.notes.append(T.NOTE_FPS_DROP.format(
                    src=info.video.fps, dst=video.fps))
            plan.notes.extend(climb_notes)
            if video.keyint:                 # the GOP follows the output rate
                out_fps = video.fps or info.video.fps or 30.0
                video.keyint = max(12, int(round(out_fps * gop_seconds)))
        return current

    if predicted_kbps <= target_video_kbps * SAMPLE_BIAS:
        video.quality = min_crf
        predicted_kbps = climb_back(predicted_kbps)
        set_peak(predicted_kbps)
        _finish_estimate(plan, info, predicted_kbps)
        plan.notes.append(T.NOTE_FITS_AT_MIN.format(
            crf=min_crf, size=human_size(plan.estimated_bytes)))
        return plan

    # The sampling run measured the real encoding speed on this machine, for
    # this content, at these settings - far better than any formula, so it is
    # kept rather than recomputed.
    return use_fixed_bitrate()
