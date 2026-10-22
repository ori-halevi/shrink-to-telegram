"""Reading everything we need to know about a source file (via ffprobe)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import ffmpeg as ff

# Codecs that can live inside an MP4 and that Telegram plays natively.
# AV1 is deliberately absent: it muxes into MP4 happily, but hardware decoding
# only exists on an A17 Pro and newer iPhone and on recent Android chips, so a
# remuxed AV1 file would simply refuse to open for most recipients.
MP4_VIDEO_OK = {"h264", "hevc", "mpeg4"}
# Pixel formats that phones and Telegram clients can actually hardware decode.
# 10 bit H.264 (High 10) and any 4:2:2 / 4:4:4 flavour play on almost nothing.
H264_COPY_PIX = {"yuv420p", "yuvj420p"}
HEVC_COPY_PIX = {"yuv420p", "yuvj420p", "yuv420p10le", "p010le"}
MP4_AUDIO_OK = {"aac", "mp3", "ac3", "eac3", "alac"}
# ...but only these are safe to copy: Telegram clients often cannot decode the rest.
MP4_AUDIO_COPY_OK = {"aac", "mp3"}
STILL_CODECS = {"png", "mjpeg", "bmp", "gif", "webp", "jpeg", "jpegls"}
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "mov_text", "text", "webvtt"}
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


def _fraction(value: Any) -> float:
    try:
        if isinstance(value, str) and "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class VideoStream:
    index: int = 0
    codec: str = ""
    profile: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bitrate_kbps: float = 0.0
    pix_fmt: str = ""
    bit_depth: int = 8
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""
    color_range: str = ""
    sar: str = ""                       # sample aspect ratio, "1:1" when square
    frames: int = 0

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in HDR_TRANSFERS

    @property
    def square_pixels(self) -> bool:
        return self.sar in ("", "1:1", "0:1", "N/A")

    @property
    def display_aspect(self) -> float:
        """Width/height as it should be shown, honouring non square pixels."""
        if not self.height:
            return 16 / 9.0
        ratio = self.width / float(self.height)
        if not self.square_pixels and ":" in self.sar:
            try:
                num, den = (float(x) for x in self.sar.split(":", 1))
                if num > 0 and den > 0:
                    ratio *= num / den
            except ValueError:
                pass
        return ratio

    @property
    def copy_safe(self) -> bool:
        """Can this stream be dropped into an MP4 untouched and still play?"""
        if self.codec == "h264":
            return self.pix_fmt in H264_COPY_PIX
        if self.codec == "hevc":
            return self.pix_fmt in HEVC_COPY_PIX
        return False                        # AV1 included - see MP4_VIDEO_OK


@dataclass
class AudioStream:
    index: int = 0
    codec: str = ""
    channels: int = 2
    bitrate_kbps: float = 0.0
    sample_rate: int = 48000
    language: str = ""
    title: str = ""
    default: bool = False


@dataclass
class SubtitleStream:
    index: int = 0
    codec: str = ""
    language: str = ""
    title: str = ""

    @property
    def is_text(self) -> bool:
        return self.codec in TEXT_SUBS


@dataclass
class MediaInfo:
    path: str
    size: int = 0
    duration: float = 0.0
    container: str = ""
    video: Optional[VideoStream] = None
    audio: List[AudioStream] = field(default_factory=list)
    subtitles: List[SubtitleStream] = field(default_factory=list)
    chapters: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_kbps(self) -> float:
        if self.duration <= 0:
            return 0.0
        return self.size * 8 / 1000.0 / self.duration

    @property
    def ext(self) -> str:
        return os.path.splitext(self.path)[1].lower()


def _pick_video(streams: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Choose the real movie stream, ignoring cover art and poster frames.

    Like ffmpeg's own default selection we prefer the largest picture, but a
    still image stream (cover art) is never the movie when a real one exists.
    """
    videos = [s for s in streams if s.get("codec_type") == "video"]
    real = []
    for stream in videos:
        if (stream.get("disposition") or {}).get("attached_pic") == 1:
            continue
        if stream.get("codec_name") in STILL_CODECS:
            continue
        real.append(stream)
    candidates = real or videos
    if not candidates:
        return None
    return max(candidates, key=lambda s: (_int(s.get("width")) * _int(s.get("height")),
                                          -_int(s.get("index"))))


def probe(tools: ff.Toolset, path: str) -> MediaInfo:
    res = ff.run(
        [
            tools.ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", "-show_chapters", path,
        ],
        timeout=180,
    )
    if not res.ok:
        raise RuntimeError(res.stderr.strip() or "ffprobe failed")
    data = json.loads(res.stdout or "{}")

    fmt = data.get("format", {})
    info = MediaInfo(path=path, raw=data)
    info.container = fmt.get("format_name", "")
    info.duration = _fraction(fmt.get("duration"))
    info.size = _int(fmt.get("size")) or (os.path.getsize(path) if os.path.isfile(path) else 0)
    info.chapters = len(data.get("chapters") or [])

    main_video = _pick_video(data.get("streams", []))
    main_index = _int(main_video.get("index"), -1) if main_video else -1

    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        tags = stream.get("tags") or {}
        disp = stream.get("disposition") or {}
        if kind == "video":
            if _int(stream.get("index"), -1) != main_index:
                continue                       # cover art or a secondary angle
            fps = _fraction(stream.get("avg_frame_rate")) or _fraction(stream.get("r_frame_rate"))
            pix_fmt = stream.get("pix_fmt", "") or ""
            depth = _int(stream.get("bits_per_raw_sample"), 0)
            if not depth:
                depth = 10 if ("10" in pix_fmt or "p010" in pix_fmt) else (12 if "12" in pix_fmt else 8)
            info.video = VideoStream(
                index=_int(stream.get("index")),
                codec=stream.get("codec_name", ""),
                profile=stream.get("profile", "") or "",
                width=_int(stream.get("width")),
                height=_int(stream.get("height")),
                fps=round(fps, 3),
                bitrate_kbps=_int(stream.get("bit_rate")) / 1000.0,
                pix_fmt=pix_fmt,
                bit_depth=depth,
                color_primaries=stream.get("color_primaries", "") or "",
                color_transfer=stream.get("color_transfer", "") or "",
                color_space=stream.get("color_space", "") or "",
                color_range=stream.get("color_range", "") or "",
                sar=stream.get("sample_aspect_ratio", "") or "",
                frames=_int(stream.get("nb_frames")),
            )
        elif kind == "audio":
            info.audio.append(
                AudioStream(
                    index=_int(stream.get("index")),
                    codec=stream.get("codec_name", ""),
                    channels=_int(stream.get("channels"), 2),
                    bitrate_kbps=_int(stream.get("bit_rate")) / 1000.0,
                    sample_rate=_int(stream.get("sample_rate"), 48000),
                    language=(tags.get("language") or "").lower(),
                    title=tags.get("title", ""),
                    default=bool(disp.get("default")),
                )
            )
        elif kind == "subtitle":
            info.subtitles.append(
                SubtitleStream(
                    index=_int(stream.get("index")),
                    codec=stream.get("codec_name", ""),
                    language=(tags.get("language") or "").lower(),
                    title=tags.get("title", ""),
                )
            )

    if info.video and info.video.bitrate_kbps <= 0 and info.duration > 0:
        # Many MKVs carry no per-stream bitrate: derive it from the file size.
        audio_kbps = sum(a.bitrate_kbps for a in info.audio)
        if info.audio and audio_kbps <= 0:
            audio_kbps = 192.0 * len(info.audio)     # unknown, assume something typical
        info.video.bitrate_kbps = max(0.0, info.total_kbps - audio_kbps)
    return info
