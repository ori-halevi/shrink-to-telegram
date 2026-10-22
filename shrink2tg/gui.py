# -*- coding: utf-8 -*-
"""The user interface: one quiet window, everything else behind a settings button."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
from typing import List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import bidi
from . import dnd
from . import encoder as enc
from . import ffmpeg as ff
from . import planner as pl
from . import probe as pb
from . import report
from . import texts as T
from . import theme
from .settings import Settings

VIDEO_TYPES = [
    ("קבצי וידאו", "*.mkv *.mp4 *.avi *.mov *.m4v *.ts *.m2ts *.webm *.wmv *.flv *.mpg *.mpeg"),
    ("כל הקבצים", "*.*"),
]
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".m2ts",
                    ".webm", ".wmv", ".flv", ".mpg", ".mpeg"}
ENCODER_CHOICES = [
    (T.ENCODER_AUTO, "auto"),
    ("libx265 (H.265 - איכות מיטבית)", "libx265"),
    ("hevc_nvenc (NVIDIA - מהיר)", "hevc_nvenc"),
    ("hevc_qsv (Intel - מהיר)", "hevc_qsv"),
    ("hevc_amf (AMD - מהיר)", "hevc_amf"),
    ("libx264 (H.264 - תאימות מרבית)", "libx264"),
]
SUB_CHOICES = [(T.SUBS_TEXT, "text"), (T.SUBS_BURN, "burn"), (T.SUBS_NONE, "none")]
MOTION_CHOICES = [(T.MOTION_SHARP, "sharpness"), (T.MOTION_SMOOTH, "smoothness")]
HEIGHTS = ["480", "576", "720", "900", "1080"]


class App(tk.Tk):
    def __init__(self, initial_files: Optional[List[str]] = None) -> None:
        self.scale = theme.enable_dpi_awareness()
        super().__init__()
        self.settings = Settings()
        self.palette = theme.apply(self, self.scale)
        self.tools: Optional[ff.Toolset] = None
        self.events: "queue.Queue" = queue.Queue()
        self.control = ff.Control()             # cancel *and* pause
        self.worker: Optional[threading.Thread] = None
        self.files: List[str] = []
        self.last_output_dir = ""
        self.log_lines: List[str] = []
        self._probe_cache: dict = {}
        self.direction_mode = bidi.AUTO
        self.status_text = T.STATUS_READY
        self.settings_window: Optional[tk.Toplevel] = None

        self.title(T.APP_TITLE)
        theme.set_window_icon(self)
        self.geometry("%dx%d" % (int(760 * self.scale), int(640 * self.scale)))
        self.minsize(int(640 * self.scale), int(540 * self.scale))
        self._make_vars()
        self._build()
        self._load_settings_into_ui()
        for path in initial_files or []:
            self._add_path(path)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.drop_enabled = dnd.enable_file_drop(self, self._files_dropped)
        self.drop_hint.set(T.HINT_DROP if self.drop_enabled else T.HINT_NO_DROP)
        self.after(100, self._pump)
        self.after(200, self._check_tools)

    # ----------------------------------------------------------- variables
    def _make_vars(self) -> None:
        """Every setting lives in a variable, whether its dialog is open or not."""
        self.target_mode = tk.StringVar(value="4")
        self.custom_gb = tk.StringVar(value="4.0")
        self.drop_hint = tk.StringVar(value="")
        self.dial = tk.IntVar(value=pl.DEFAULT_LEVEL)
        self.dial_value = tk.StringVar(value="")
        self.dial_note = tk.StringVar(value="")
        self.dial_time = tk.StringVar(value="")
        self.status = tk.StringVar(value=T.STATUS_READY)
        self.eta = tk.StringVar(value="")
        self.ffmpeg_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.encoder_label = tk.StringVar()
        self.preset = tk.StringVar()
        self.subs_label = tk.StringVar()
        self.motion_label = tk.StringVar()
        self.min_crf = tk.StringVar()
        self.max_crf = tk.StringVar()
        self.min_height = tk.StringVar()
        self.margin = tk.StringVar()
        self.audio_kbps = tk.StringVar()
        self.keyframe_seconds = tk.StringVar()
        self.deep = tk.BooleanVar()
        self.use_profile = tk.BooleanVar()
        self.quality_check = tk.BooleanVar()
        self.resolution_ab = tk.BooleanVar()
        self.downscale = tk.BooleanVar()
        self.fps_drop = tk.BooleanVar()
        self.keep_all_audio = tk.BooleanVar()
        self.open_when_done = tk.BooleanVar()

    # --------------------------------------------------------------- layout
    def _px(self, value: float) -> int:
        return int(round(value * self.scale))

    def _card(self, parent, **pack) -> ttk.Frame:
        """A surface panel with a hairline border - Tk has no such widget."""
        holder = tk.Frame(parent, background=self.palette.border, bd=0,
                          highlightthickness=0)
        holder.pack(fill="x", **pack)
        card = ttk.Frame(holder, style="Card.TFrame", padding=self._px(12))
        card.pack(fill="both", expand=True, padx=1, pady=1)
        card.holder = holder                    # so callers can re-pack it
        return card

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=(self._px(16), self._px(14)))
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x")
        self.btn_settings = ttk.Button(head, text=T.BTN_SETTINGS,
                                       command=self._open_settings)
        self.btn_settings.pack(side="left")
        titles = ttk.Frame(head)
        titles.pack(side="right")
        ttk.Label(titles, text=T.APP_NAME, style="Title.TLabel").pack(anchor="e")
        ttk.Label(titles, text=bidi.as_label(T.APP_TAGLINE), style="Muted.TLabel").pack(anchor="e")

        # --- files ---------------------------------------------------------
        files = self._card(outer, pady=(self._px(14), 0))
        row = ttk.Frame(files, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text=bidi.as_label(T.LBL_FILES), style="Card.TLabel").pack(side="right")
        ttk.Button(row, text=T.BTN_CLEAR, style="Quiet.TButton",
                   command=self._clear_files).pack(side="left")
        ttk.Button(row, text=T.BTN_ADD_FILES, style="Quiet.TButton",
                   command=self._add_files).pack(side="left", padx=(0, self._px(6)))

        table = ttk.Frame(files, style="Card.TFrame")
        table.pack(fill="both", expand=True, pady=(self._px(8), self._px(4)))
        self.file_list = ttk.Treeview(table, columns=("size",), show="tree", height=5,
                                      selectmode="extended")
        self.file_list.column("#0", anchor="e", stretch=True)
        self.file_list.column("size", anchor="w", width=self._px(90), stretch=False)
        self.file_list.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(table, orient="vertical", command=self.file_list.yview)
        bar.pack(side="left", fill="y")
        self.file_list.configure(yscrollcommand=bar.set)
        self.file_list.bind("<Delete>", lambda _event: self._remove_selected())
        ttk.Label(files, textvariable=self.drop_hint,
                  style="CardMuted.TLabel").pack(anchor="e")

        # --- target --------------------------------------------------------
        target = self._card(outer, pady=(self._px(10), 0))
        line = ttk.Frame(target, style="Card.TFrame")
        line.pack(fill="x")
        ttk.Label(line, text=bidi.as_label(T.LBL_TARGET), style="Card.TLabel").pack(
            side="right", padx=(0, self._px(14)))
        for text, value in ((T.LBL_TARGET_4, "4"), (T.LBL_TARGET_2, "2"),
                            (T.LBL_TARGET_CUSTOM, "custom")):
            ttk.Radiobutton(line, text=bidi.as_label(text), value=value,
                            variable=self.target_mode,
                            command=self._target_changed).pack(
                side="right", padx=(0, self._px(10)))
        self.custom_entry = ttk.Entry(line, textvariable=self.custom_gb, width=5,
                                      justify="center")
        self.custom_entry.pack(side="right")

        # --- quality dial ---------------------------------------------------
        dial = self._card(outer, pady=(self._px(10), 0))
        head_row = ttk.Frame(dial, style="Card.TFrame")
        head_row.pack(fill="x")
        ttk.Label(head_row, text=bidi.as_label(T.LBL_DIAL),
                  style="Card.TLabel").pack(side="right")
        ttk.Label(head_row, textvariable=self.dial_value,
                  style="CardMuted.TLabel").pack(side="left")
        slider_row = ttk.Frame(dial, style="Card.TFrame")
        slider_row.pack(fill="x", pady=(self._px(6), 0))
        ttk.Label(slider_row, text=T.DIAL_BEST,
                  style="CardMuted.TLabel").pack(side="right")
        ttk.Label(slider_row, text=T.DIAL_FAST,
                  style="CardMuted.TLabel").pack(side="left")
        self.dial_scale = ttk.Scale(
            slider_row, from_=1, to=len(pl.QUALITY_LADDER), orient="horizontal",
            command=self._dial_moved)
        self.dial_scale.pack(side="right", fill="x", expand=True,
                             padx=(self._px(10), self._px(10)))
        self.dial_scale.set(self.dial.get())
        ttk.Label(dial, textvariable=self.dial_note, style="CardMuted.TLabel",
                  anchor="e", justify="right", wraplength=self._px(600)).pack(
            fill="x", anchor="e", pady=(self._px(6), 0))
        ttk.Label(dial, textvariable=self.dial_time, style="Card.TLabel",
                  anchor="e").pack(fill="x", anchor="e")
        theme.Tooltip(self.dial_scale, self._dial_tooltip, self.palette, self.scale)

        # --- actions -------------------------------------------------------
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(self._px(14), 0))
        self.btn_start = ttk.Button(actions, text=T.BTN_START, style="Accent.TButton",
                                    command=self._start)
        self.btn_start.pack(side="right")
        self.btn_analyze = ttk.Button(actions, text=T.BTN_ANALYZE,
                                      command=lambda: self._start(analyze_only=True))
        self.btn_analyze.pack(side="right", padx=(0, self._px(8)))
        self.btn_open = ttk.Button(actions, text=T.BTN_OPEN_FOLDER, style="Quiet.TButton",
                                   command=self._open_output_folder, state="disabled")
        self.btn_open.pack(side="left")
        self.btn_stop = ttk.Button(actions, text=T.BTN_STOP, command=self._stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=(self._px(8), 0))
        self.btn_pause = ttk.Button(actions, text=T.BTN_PAUSE,
                                    command=self._toggle_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=(self._px(8), 0))

        # --- progress ------------------------------------------------------
        progress = ttk.Frame(outer)
        progress.pack(fill="x", pady=(self._px(14), 0))
        self.progress = ttk.Progressbar(progress, maximum=100.0,
                                        style="Thin.Horizontal.TProgressbar")
        self.progress.pack(fill="x")
        under = ttk.Frame(progress)
        under.pack(fill="x", pady=(self._px(6), 0))
        self.status_label = ttk.Label(under, textvariable=self.status, anchor="e")
        self.status_label.pack(side="right")
        self.eta_label = ttk.Label(under, textvariable=self.eta, style="Muted.TLabel",
                                   anchor="w")
        self.eta_label.pack(side="left")

        # --- log -----------------------------------------------------------
        log_card = self._card(outer, pady=(self._px(12), 0))
        log_card.holder.pack_configure(fill="both", expand=True)
        log_head = ttk.Frame(log_card, style="Card.TFrame")
        log_head.pack(fill="x")
        ttk.Label(log_head, text=bidi.as_label(T.LBL_LOG), style="Card.TLabel").pack(side="right")
        self.btn_direction = ttk.Button(log_head, style="Quiet.TButton",
                                        command=self._cycle_direction)
        self.btn_direction.pack(side="left")
        self._update_direction_button()
        body = ttk.Frame(log_card, style="Card.TFrame")
        body.pack(fill="both", expand=True, pady=(self._px(6), 0))
        self.log = tk.Text(body, height=8, state="disabled")
        theme.style_text(self.log, self.palette, self.scale)
        self.log.pack(side="left", fill="both", expand=True)
        log_bar = ttk.Scrollbar(body, orient="vertical", command=self.log.yview)
        log_bar.pack(side="left", fill="y")
        self.log.configure(yscrollcommand=log_bar.set)
        self.log.tag_configure(bidi.RTL, justify="right")
        self.log.tag_configure(bidi.LTR, justify="left")

    # ------------------------------------------------------- settings window
    def _open_settings(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return                   # the worker thread is reading the settings
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        win = tk.Toplevel(self)
        self.settings_window = win
        win.title(T.BTN_SETTINGS)
        win.configure(background=self.palette.bg)
        win.transient(self)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=self._px(16))
        frame.pack(fill="both", expand=True)

        def section(title: str) -> ttk.Frame:
            ttk.Label(frame, text=bidi.as_label(title), style="Muted.TLabel").pack(
                anchor="e", pady=(self._px(10), self._px(4)))
            holder = tk.Frame(frame, background=self.palette.border, bd=0,
                              highlightthickness=0)
            holder.pack(fill="x")
            inner = ttk.Frame(holder, style="Card.TFrame", padding=self._px(10))
            inner.pack(fill="both", expand=True, padx=1, pady=1)
            inner.columnconfigure(1, weight=1)
            return inner

        def field(parent, row: int, label: str, widget, button=None,
                  stretch: bool = True) -> None:
            ttk.Label(parent, text=bidi.as_label(label), style="Card.TLabel").grid(
                row=row, column=2, sticky="e", padx=(self._px(8), 0), pady=self._px(3))
            widget.grid(row=row, column=1, sticky="ew" if stretch else "w",
                        pady=self._px(3))
            if button is not None:
                button.grid(row=row, column=0, sticky="w", padx=(self._px(6), 0))

        tools = section(T.SEC_TOOLS)
        ffmpeg_entry = ttk.Entry(tools, textvariable=self.ffmpeg_path)
        ffmpeg_entry.after(50, lambda: ffmpeg_entry.xview_moveto(1.0))
        field(tools, 0, T.LBL_FFMPEG, ffmpeg_entry,
              ttk.Button(tools, text=T.BTN_BROWSE, style="Quiet.TButton",
                         command=self._browse_ffmpeg))
        field(tools, 1, T.LBL_OUTPUT_DIR, ttk.Entry(tools, textvariable=self.output_dir),
              ttk.Button(tools, text=T.BTN_BROWSE, style="Quiet.TButton",
                         command=self._browse_output))

        encode = section(T.SEC_ENCODE)
        field(encode, 0, T.LBL_ENCODER,
              ttk.Combobox(encode, textvariable=self.encoder_label, state="readonly",
                           width=30, values=[name for name, _ in ENCODER_CHOICES]),
              stretch=False)
        field(encode, 2, T.LBL_MOTION,
              ttk.Combobox(encode, textvariable=self.motion_label, state="readonly",
                           width=26, values=[name for name, _ in MOTION_CHOICES]),
              stretch=False)
        quality = ttk.Frame(encode, style="Card.TFrame")
        ttk.Spinbox(quality, from_=10, to=40, width=4,
                    textvariable=self.min_crf).pack(side="right")
        ttk.Label(quality, text="  עד  ", style="Card.TLabel").pack(side="right")
        ttk.Spinbox(quality, from_=12, to=45, width=4,
                    textvariable=self.max_crf).pack(side="right")
        field(encode, 3, T.LBL_QUALITY_RANGE, quality, stretch=False)
        limits = ttk.Frame(encode, style="Card.TFrame")
        ttk.Combobox(limits, textvariable=self.min_height, state="readonly", width=6,
                     values=HEIGHTS).pack(side="right")
        ttk.Label(limits, text="     ", style="Card.TLabel").pack(side="right")
        ttk.Spinbox(limits, from_=0, to=10, increment=0.5, width=4,
                    textvariable=self.keyframe_seconds).pack(side="right")
        field(encode, 4, T.LBL_MIN_HEIGHT, limits, stretch=False)

        analysis = section(T.SEC_ANALYSIS)
        analysis.columnconfigure(0, weight=1)
        analysis.columnconfigure(2, weight=1)
        switches = ((T.LBL_DEEP, self.deep), (T.LBL_PROFILE, self.use_profile),
                    (T.LBL_QUALITY_CHECK, self.quality_check),
                    (T.LBL_RES_AB, self.resolution_ab),
                    (T.LBL_DOWNSCALE, self.downscale),
                    (T.LBL_FPS_DROP, self.fps_drop))
        for index, (text, var) in enumerate(switches):
            theme.Check(analysis, bidi.as_label(text), var, self.palette,
                        self.scale).grid(row=index // 2,
                                         column=2 if index % 2 == 0 else 0,
                                         sticky="e", pady=self._px(2))

        streams = section(T.SEC_STREAMS)
        field(streams, 0, T.LBL_SUBS,
              ttk.Combobox(streams, textvariable=self.subs_label, state="readonly",
                           width=24, values=[name for name, _ in SUB_CHOICES]),
              stretch=False)
        field(streams, 1, T.LBL_AUDIO_KBPS,
              ttk.Spinbox(streams, from_=64, to=384, increment=16, width=6,
                          textvariable=self.audio_kbps), stretch=False)
        field(streams, 2, T.LBL_MARGIN,
              ttk.Spinbox(streams, from_=0, to=15, increment=0.5, width=6,
                          textvariable=self.margin), stretch=False)
        for index, (text, var) in enumerate(((T.LBL_KEEP_ALL_AUDIO, self.keep_all_audio),
                                             (T.LBL_OPEN_WHEN_DONE, self.open_when_done))):
            theme.Check(streams, bidi.as_label(text), var, self.palette,
                        self.scale).grid(row=3 + index, column=0, columnspan=3,
                                         sticky="e", pady=self._px(2))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(self._px(16), 0))
        ttk.Button(buttons, text=T.BTN_SAVE, style="Accent.TButton",
                   command=lambda: self._close_settings(save=True)).pack(side="right")
        ttk.Button(buttons, text=T.BTN_CANCEL,
                   command=lambda: self._close_settings(save=False)).pack(
            side="right", padx=(0, self._px(8)))

        win.protocol("WM_DELETE_WINDOW", lambda: self._close_settings(save=True))
        win.update_idletasks()
        win.geometry("+%d+%d" % (self.winfo_rootx() + self._px(40),
                                 self.winfo_rooty() + self._px(40)))
        self._target_changed()

    def _close_settings(self, save: bool) -> None:
        if save:
            self._collect_settings()
        else:
            self._load_settings_into_ui()
        if self.settings_window is not None:
            self.settings_window.destroy()
            self.settings_window = None

    # ------------------------------------------------------------ settings io
    def _load_settings_into_ui(self) -> None:
        s = self.settings
        gb = float(s["target_gb"])
        self.target_mode.set("4" if abs(gb - 4) < 0.01
                             else ("2" if abs(gb - 2) < 0.01 else "custom"))
        self.custom_gb.set("%g" % gb)
        self.ffmpeg_path.set(s["ffmpeg_path"])
        self.output_dir.set(s["output_dir"])
        self.encoder_label.set(_label_for(ENCODER_CHOICES, s["encoder"]))
        self.preset.set(s["preset"])
        self.subs_label.set(_label_for(SUB_CHOICES, s["subtitles"]))
        self.motion_label.set(_label_for(MOTION_CHOICES, s["motion_priority"]))
        self.min_crf.set(str(s["min_crf"]))
        self.max_crf.set(str(s["max_crf"]))
        self.min_height.set(str(s["min_height"]))
        self.margin.set("%g" % float(s["safety_margin_pct"]))
        self.audio_kbps.set(str(s["audio_max_kbps"]))
        self.keyframe_seconds.set("%g" % float(s["keyframe_seconds"]))
        self.dial.set(int(s["quality_level"]))
        try:
            self.dial_scale.set(int(s["quality_level"]))
        except Exception:
            pass
        self.deep.set(bool(s["deep_analysis"]))
        self.use_profile.set(bool(s["use_profile"]))
        self.quality_check.set(bool(s["quality_check"]))
        self.resolution_ab.set(bool(s["resolution_ab_test"]))
        self.downscale.set(bool(s["allow_downscale"]))
        self.fps_drop.set(bool(s["allow_fps_drop"]))
        self.keep_all_audio.set(bool(s["keep_all_audio"]))
        self.open_when_done.set(bool(s["open_folder_when_done"]))
        self._target_changed()

    def _collect_settings(self) -> None:
        s = self.settings
        mode = self.target_mode.get()
        if mode == "custom":
            try:
                s["target_gb"] = max(0.05, float(self.custom_gb.get().replace(",", ".")))
            except ValueError:
                s["target_gb"] = 4.0
        else:
            s["target_gb"] = float(mode)
        s["ffmpeg_path"] = self.ffmpeg_path.get().strip()
        s["output_dir"] = self.output_dir.get().strip()
        s["encoder"] = _value_for(ENCODER_CHOICES, self.encoder_label.get(), "auto")
        s["preset"] = self.preset.get() or "medium"
        s["subtitles"] = _value_for(SUB_CHOICES, self.subs_label.get(), "text")
        s["motion_priority"] = _value_for(MOTION_CHOICES, self.motion_label.get(),
                                          "sharpness")
        s["min_crf"] = _as_int(self.min_crf.get(), 18, 10, 40)
        s["max_crf"] = max(_as_int(self.max_crf.get(), 30, 12, 45), s["min_crf"])
        s["min_height"] = _as_int(self.min_height.get(), 720, 240, 2160)
        s["safety_margin_pct"] = _as_float(self.margin.get(), 2.0, 0.0, 20.0)
        s["audio_max_kbps"] = _as_int(self.audio_kbps.get(), 192, 64, 384)
        s["keyframe_seconds"] = _as_float(self.keyframe_seconds.get(), 2.0, 0.0, 10.0)
        s["quality_level"] = max(1, min(len(pl.QUALITY_LADDER),
                                        int(self.dial.get())))
        s["deep_analysis"] = bool(self.deep.get())
        s["use_profile"] = bool(self.use_profile.get())
        s["quality_check"] = bool(self.quality_check.get())
        s["resolution_ab_test"] = bool(self.resolution_ab.get())
        s["allow_downscale"] = bool(self.downscale.get())
        s["allow_fps_drop"] = bool(self.fps_drop.get())
        s["keep_all_audio"] = bool(self.keep_all_audio.get())
        s["open_folder_when_done"] = bool(self.open_when_done.get())
        s.save()

    # ----------------------------------------------------------- the dial
    def _dial_moved(self, raw) -> None:
        """ttk.Scale is continuous, so snap it to whole stops."""
        try:
            level = int(round(float(raw)))
        except (TypeError, ValueError):
            return
        level = max(1, min(len(pl.QUALITY_LADDER), level))
        if level != self.dial.get():
            self.dial.set(level)
        if abs(float(self.dial_scale.get()) - level) > 0.01:
            self.dial_scale.set(level)
        self._refresh_dial()

    def _dial_tooltip(self) -> str:
        item = pl.quality_level(self.dial.get())
        return (T.DIAL_TIP_HEAD + "\n\n" + T.DIAL_TIP_BODY
                + T.DIAL_TIP_LEVEL.format(level=item.level, name=item.name,
                                          note=item.note, vmaf=item.vmaf))

    def _refresh_dial(self) -> None:
        """Keep the labels in step with the slider and with the file list."""
        item = pl.quality_level(self.dial.get())
        self.dial_value.set(bidi.as_label(
            T.DIAL_VALUE.format(level=item.level, name=item.name)))
        note, total, known, advice = item.note, 0.0, 0, ""
        for info in self._infos():
            total += pl.level_seconds(item, info)
            known += 1
            if known == 1:
                level, why = pl.recommend_level(
                    info, self._target_gb(),
                    float(self.settings["safety_margin_pct"]),
                    float(self.settings["h264_cut_ratio"]))
                advice = "%s %s" % (T.DIAL_RECOMMEND.format(level=level), why)
        if not known:
            self.dial_time.set(bidi.as_label(T.DIAL_TIME_NONE))
        elif known == 1:
            self.dial_time.set(bidi.as_label(
                T.DIAL_TIME.format(time=pl.human_time(total))))
        else:
            self.dial_time.set(bidi.as_label(T.DIAL_TIME_MANY.format(
                count=known, time=pl.human_time(total))))
        if advice:
            note = advice + "  -  " + note
        self.dial_note.set(bidi.as_label(note))

    def _target_gb(self) -> float:
        mode = self.target_mode.get()
        if mode == "custom":
            try:
                return max(0.05, float(self.custom_gb.get().replace(",", ".")))
            except ValueError:
                return 4.0
        try:
            return float(mode)
        except ValueError:
            return 4.0

    def _infos(self):
        """Cached probes, so dragging the dial stays instant."""
        out = []
        for path in self.files:
            info = self._probe_cache.get(path)
            if info is None:
                if self.tools is None:
                    continue
                try:
                    info = pb.probe(self.tools, path)
                except Exception:
                    info = False
                self._probe_cache[path] = info
            if info:
                out.append(info)
        return out

    def _target_changed(self) -> None:
        state = "normal" if self.target_mode.get() == "custom" else "disabled"
        try:
            self.custom_entry.configure(state=state)
        except tk.TclError:
            pass
        self._refresh_dial()

    # --------------------------------------------------------------- files
    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title=T.BTN_ADD_FILES, filetypes=VIDEO_TYPES,
            initialdir=self.settings["last_input_dir"] or os.path.expanduser("~"))
        for path in paths:
            self._add_path(path)
        if paths:
            self.settings["last_input_dir"] = os.path.dirname(paths[-1])
            self.settings.save()

    def _add_path(self, path: str) -> None:
        path = os.path.abspath(path)
        if not os.path.isfile(path) or path in self.files:
            return
        self.files.append(path)
        try:
            size = pl.human_size(os.path.getsize(path))
        except OSError:
            size = "-"
        self.file_list.insert("", "end", iid=path,
                              text=os.path.basename(path), values=(size,))
        self._refresh_dial()

    def _remove_selected(self) -> None:
        for item in self.file_list.selection():
            if item in self.files:
                self.files.remove(item)
            self.file_list.delete(item)
        self._refresh_dial()

    def _clear_files(self) -> None:
        self.files.clear()
        self.file_list.delete(*self.file_list.get_children())
        self._refresh_dial()

    def _files_dropped(self, paths: List[str]) -> None:
        added = 0
        for path in paths:
            if os.path.isdir(path):             # a dropped folder brings its videos
                for name in sorted(os.listdir(path)):
                    if os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
                        before = len(self.files)
                        self._add_path(os.path.join(path, name))
                        added += len(self.files) - before
                continue
            before = len(self.files)
            self._add_path(path)
            added += len(self.files) - before
        if added:
            self._log(T.HINT_DROPPED.format(count=added))
            # A dropped folder is itself the place to reopen, not its parent.
            last = paths[-1]
            self.settings["last_input_dir"] = (
                last if os.path.isdir(last) else os.path.dirname(last))

    def _browse_ffmpeg(self) -> None:
        path = filedialog.askopenfilename(
            title=T.LBL_FFMPEG, filetypes=[("ffmpeg", "ffmpeg.exe"), ("הכול", "*.*")])
        if path:
            self.ffmpeg_path.set(path)
            self.tools = None
            self._check_tools()

    def _browse_output(self) -> None:
        path = filedialog.askdirectory(title=T.LBL_OUTPUT_DIR)
        if path:
            self.output_dir.set(path)

    def _open_output_folder(self) -> None:
        folder = self.last_output_dir or self.settings["output_dir"]
        if folder and os.path.isdir(folder):
            try:
                os.startfile(folder)                       # type: ignore[attr-defined]
            except AttributeError:
                subprocess.Popen(["xdg-open", folder])

    # ---------------------------------------------------------------- tools
    def _check_tools(self) -> bool:
        try:
            self.tools = ff.load_toolset(self.ffmpeg_path.get().strip(), "")
        except Exception:
            self.tools = None
            self._set_status(T.STATUS_NO_FFMPEG)
            self._open_settings()
            return False
        if not self.ffmpeg_path.get().strip():
            self.ffmpeg_path.set(self.tools.ffmpeg)
        self._set_status(T.STATUS_READY)
        self._log(self.tools.version or self.tools.ffmpeg)
        # Files may already be listed (dropped onto run.bat); now that ffprobe
        # is available the dial can finally price them.
        self._refresh_dial()
        return True

    # --------------------------------------------------------------- events
    def _post(self, kind: str, *payload) -> None:
        self.events.put((kind,) + payload)

    def _pump(self) -> None:
        try:
            while True:
                kind, *payload = self.events.get_nowait()
                if kind == "status":
                    self._set_status(payload[0])
                elif kind == "eta":
                    self.eta.set(bidi.render(payload[0], self.direction_mode)[0])
                elif kind == "progress":
                    self.progress["value"] = max(0.0, min(100.0, payload[0] * 100.0))
                elif kind == "log":
                    self._log(payload[0])
                elif kind == "done":
                    self._finish(payload[0] if payload else "")
        except queue.Empty:
            pass
        except Exception:                      # never let the UI pump die
            try:
                self._log(traceback.format_exc())
            except Exception:
                pass                           # logging must not kill the pump
        finally:
            self.after(100, self._pump)

    # ------------------------------------------------------------------ log
    def _log(self, text: str) -> None:
        for line in str(text).split("\n"):
            self.log_lines.append(line)
            self._write_line(line)
        self.log.see("end")

    def _write_line(self, line: str) -> None:
        rendered, direction = bidi.render(line, self.direction_mode)
        self.log.configure(state="normal")
        self.log.insert("end", rendered + "\n", direction)
        self.log.configure(state="disabled")

    def _cycle_direction(self) -> None:
        """Flip the whole log between automatic, forced RTL and forced LTR."""
        order = bidi.MODES
        self.direction_mode = order[(order.index(self.direction_mode) + 1) % len(order)]
        self._update_direction_button()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        for line in self.log_lines:
            self._write_line(line)
        self.log.see("end")
        self._set_status(self.status_text)

    def _update_direction_button(self) -> None:
        labels = {bidi.AUTO: T.DIR_AUTO, bidi.RTL: T.DIR_RTL, bidi.LTR: T.DIR_LTR}
        self.btn_direction.configure(
            text=T.BTN_DIRECTION.format(mode=labels[self.direction_mode]))

    def _set_status(self, text: str) -> None:
        """Show a status line, aligned the way its own language reads."""
        self.status_text = text
        rendered, direction = bidi.render(text, self.direction_mode)
        self.status.set(rendered)
        self.status_label.configure(anchor="e" if direction == bidi.RTL else "w")

    # ----------------------------------------------------------------- run
    def _start(self, analyze_only: bool = False) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.files:
            messagebox.showinfo(T.APP_NAME, T.HINT_NO_FILES)
            return
        # From here the worker thread reads the settings, so freeze them first:
        # close the dialog (keeping what is in it) and lock the button.
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self._close_settings(save=True)
        else:
            self._collect_settings()
        if not self._check_tools():
            return
        self.control.clear()
        self.progress["value"] = 0
        self.btn_settings.configure(state="disabled")
        self.btn_start.configure(state="disabled")
        self.btn_analyze.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_pause.configure(state="normal", text=T.BTN_PAUSE)
        files = list(self.files)
        self.worker = threading.Thread(target=self._work, args=(files, analyze_only),
                                       daemon=True)
        self.worker.start()

    def _toggle_pause(self) -> None:
        if not (self.worker and self.worker.is_alive()):
            return
        paused = self.control.toggle_pause()
        self.btn_pause.configure(text=T.BTN_RESUME if paused else T.BTN_PAUSE)
        self._set_status(T.STATUS_PAUSED if paused else T.STATUS_RESUMED)

    def _stop(self) -> None:
        self.control.request_cancel()
        self.btn_pause.configure(text=T.BTN_PAUSE, state="disabled")
        self._set_status(T.STATUS_CANCELLED)

    def _finish(self, message: str) -> None:
        self.btn_settings.configure(state="normal")
        self.btn_start.configure(state="normal")
        self.btn_analyze.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.btn_pause.configure(state="disabled", text=T.BTN_PAUSE)
        if message:
            self._set_status(message)
        self.eta.set("")
        if self.last_output_dir:
            self.btn_open.configure(state="normal")

    def _work(self, files: List[str], analyze_only: bool) -> None:
        settings = self.settings
        assert self.tools is not None
        ok_count = 0
        try:
            for index, path in enumerate(files):
                if self.control.is_set():
                    break
                name = os.path.basename(path)
                self._post("status", T.STATUS_ANALYZING.format(name=name))
                self._post("progress", index / float(len(files)))
                try:
                    info = pb.probe(self.tools, path)
                    plan = pl.build_plan(self.tools, info, settings,
                                         on_status=lambda text: self._post("status", text),
                                         cancel=self.control)
                except pl.Cancelled:
                    break
                except pl.SampleFailed as exc:
                    self._post("log", T.ERR_SAMPLE_FAILED.format(msg=exc))
                    self._post("status", T.STATUS_FAILED.format(name=name))
                    continue
                except Exception as exc:                       # noqa: BLE001
                    # Hebrew first, so the line reads right to left as one piece.
                    self._post("log", "%s (%s)" % (exc, name))
                    self._post("status", T.STATUS_FAILED.format(name=name))
                    continue
                self._post("log", "")
                self._post("log", report.describe(plan, settings))
                if analyze_only:
                    ok_count += 1
                    continue

                base = index / float(len(files))
                span = 1.0 / float(len(files))

                def on_progress(fraction: float, speed: float, eta: float,
                                base=base, span=span) -> None:
                    self._post("progress", base + fraction * span)
                    if eta > 0:
                        self._post("eta", T.STATUS_ETA.format(eta=pl.human_time(eta),
                                                              speed=speed))

                result = enc.run_plan(
                    self.tools, plan, settings, on_progress=on_progress,
                    on_status=lambda text: self._post("status", text),
                    on_log=None, cancel=self.control,
                    on_note=lambda line: self._post("log", line),
                )
                if result.cancelled:
                    break
                if result.ok:
                    ok_count += 1
                    self.last_output_dir = os.path.dirname(result.output)
                    done = T.STATUS_DONE.format(name=os.path.basename(result.output),
                                                size=pl.human_size(result.size))
                    self._post("log", done)
                    self._post("status", done)
                else:
                    # An over-budget result is reported as a failure but the
                    # file itself was deliberately kept - point the folder
                    # button at it instead of leaving it unreachable.
                    if result.output and os.path.isfile(result.output):
                        self.last_output_dir = os.path.dirname(result.output)
                    self._post("log", T.ERR_FFMPEG.format(msg=result.message))
                    self._post("status", T.STATUS_FAILED.format(name=name))
                self._post("progress", base + span)
        except Exception:                                      # noqa: BLE001
            self._post("log", traceback.format_exc())
        finally:
            if self.control.is_set():
                self._post("done", T.STATUS_CANCELLED)
            else:
                self._post("progress", 1.0)
                if ok_count and not analyze_only:
                    self._post("log", T.HINT_SEND_ORIGINAL)
                self._post("done", T.STATUS_QUEUE_DONE.format(ok=ok_count,
                                                              total=len(files)))
                if (not analyze_only and ok_count and self.last_output_dir
                        and bool(settings["open_folder_when_done"])):
                    try:
                        os.startfile(self.last_output_dir)     # type: ignore[attr-defined]
                    except Exception:
                        pass

    def _on_close(self) -> None:
        try:
            self._collect_settings()
        except Exception:
            pass
        self.control.request_cancel()
        self.destroy()


def _label_for(choices, value, default="") -> str:
    for label, val in choices:
        if val == value:
            return label
    return choices[0][0] if choices else default


def _value_for(choices, label, default) -> str:
    for name, val in choices:
        if name == label:
            return val
    return default


def _as_int(text: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(float(text))))
    except (TypeError, ValueError):
        return default


def _as_float(text: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(str(text).replace(",", "."))))
    except (TypeError, ValueError):
        return default


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    app = App(initial_files=[a for a in args if not a.startswith("-")])
    app.mainloop()
    return 0
