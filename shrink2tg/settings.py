"""Portable user settings for Shrink-To-Telegram.

Settings live in ``settings.json`` next to the application folder so the whole
thing can be copied to a USB stick.  If that location is not writable we fall
back to ``%APPDATA%/ShrinkToTelegram``.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

APP_NAME = "ShrinkToTelegram"

DEFAULTS: Dict[str, Any] = {
    # --- tools -------------------------------------------------------------
    "ffmpeg_path": "",            # empty = auto detect
    "ffprobe_path": "",           # empty = derive from ffmpeg / auto detect
    # --- output ------------------------------------------------------------
    "target_gb": 4.0,             # 4 GB (Telegram Premium) or 2 GB (free)
    "safety_margin_pct": 2.0,     # aim a little below the limit
    "output_dir": "",             # empty = next to the source file
    "output_suffix": "-telegram",
    # --- encoding ----------------------------------------------------------
    "encoder": "auto",            # auto | libx265 | libx264 | hevc_nvenc | hevc_qsv | hevc_amf
    "h264_cut_ratio": 0.55,       # auto: shallower cut than this stays on H.264 (0 = always HEVC)
    "quality_level": 4,           # 1 = fastest .. 7 = extreme; drives preset + two-pass + codec
    "preset": "medium",           # only used for hardware encoders now; the dial drives the rest
    "min_crf": 18,                # never go below this quality
    "max_crf": 30,                # ceiling for the self-correcting retries
    "allow_downscale": True,
    "min_height": 720,            # do not shrink the picture below this
    "allow_fps_drop": True,       # last resort before quality collapses
    "motion_priority": "sharpness",   # sharpness = drop fps first, smoothness = drop size first
    "keyframe_seconds": 4.0,      # GOP length; short = better seeking, costs bitrate
    "deep_analysis": True,        # sample test-encodes for an accurate plan
    "use_profile": True,          # cheap packet scan to place samples and debias them
    "quality_check": True,        # measure VMAF of the finished file (long jobs only)
    "resolution_ab_test": False,   # decide downscaling by measuring, not by rule of thumb
    "sample_count": 6,
    "sample_seconds": 10,
    # --- streams -----------------------------------------------------------
    "keep_all_audio": False,
    "audio_max_kbps": 256,        # ceiling per track
    "audio_budget_share": 0.12,   # audio may take this share of the whole budget
    "subtitles": "none",          # text | burn | none
    "tonemap_hdr": False,
    # --- misc --------------------------------------------------------------
    "last_input_dir": "",
    "open_folder_when_done": True,
}


def _app_dir() -> str:
    if getattr(sys, "frozen", False):                     # PyInstaller build
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _candidate_paths() -> list[str]:
    paths = [os.path.join(_app_dir(), "settings.json")]
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(os.path.join(appdata, APP_NAME, "settings.json"))
    return paths


class Settings:
    def __init__(self) -> None:
        self.path = _candidate_paths()[0]
        self.data: Dict[str, Any] = dict(DEFAULTS)
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> None:
        for path in _candidate_paths():
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        stored = json.load(fh)
                    if isinstance(stored, dict):
                        self.data.update({k: v for k, v in stored.items() if k in DEFAULTS})
                        self.path = path
                        return
                except (OSError, ValueError):
                    continue

    def save(self) -> None:
        for path in _candidate_paths():
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh, indent=2, ensure_ascii=False)
                self.path = path
                return
            except OSError:
                continue

    # --------------------------------------------------------------- access
    def __getitem__(self, key: str) -> Any:
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, DEFAULTS.get(key, default))

    def update(self, **kwargs: Any) -> None:
        self.data.update(kwargs)
