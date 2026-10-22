# -*- coding: utf-8 -*-
"""Making plain Tkinter look like it belongs on this decade's Windows.

No extra packages: the whole difference comes from four things Tk gives us for
free and nobody bothers to set - real DPI awareness, the system font, a flat
colour scheme taken from the user's own Windows theme, and enough padding.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from tkinter import TclError
from tkinter import font as tkfont
from tkinter import ttk

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"


# ----------------------------------------------------------------- windows
def enable_dpi_awareness() -> float:
    """Ask Windows to stop stretching our pixels. Returns the DPI scale."""
    if sys.platform != "win32":
        return 1.0
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # system aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            return 1.0
    try:
        dc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)     # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, dc)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


def _registry(path: str, name: str):
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except Exception:
        return None


def system_dark() -> bool:
    value = _registry(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                      "AppsUseLightTheme")
    return value == 0


def system_accent(default: str = "#2f7fd4") -> str:
    value = _registry(r"Software\Microsoft\Windows\DWM", "AccentColor")
    if not isinstance(value, int):
        return default
    # Stored as 0xAABBGGRR.
    blue, green, red = (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
    return "#%02x%02x%02x" % (red, green, blue)


# ------------------------------------------------------------------ colours
def _rgb(color: str) -> tuple:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def mix(first: str, second: str, amount: float) -> str:
    """Blend two colours; ``amount`` 0 keeps ``first``, 1 gives ``second``."""
    a, b = _rgb(first), _rgb(second)
    return "#%02x%02x%02x" % tuple(
        int(round(a[i] + (b[i] - a[i]) * amount)) for i in range(3))


def readable_on(color: str) -> str:
    red, green, blue = _rgb(color)
    return "#101014" if (red * 0.299 + green * 0.587 + blue * 0.114) > 165 else "#ffffff"


@dataclass
class Palette:
    dark: bool
    bg: str
    surface: str
    border: str
    text: str
    muted: str
    field: str
    accent: str
    accent_text: str
    accent_hover: str
    hover: str
    ok: str
    warn: str


def build_palette(dark: bool, accent: str) -> Palette:
    if dark:
        bg, surface, field = "#1d1d20", "#26262b", "#2f2f35"
        border, text, muted = "#3a3a42", "#eaeaee", "#9b9ba4"
    else:
        bg, surface, field = "#f4f4f6", "#ffffff", "#ffffff"
        border, text, muted = "#d8d8de", "#1b1b1f", "#6a6a74"
    return Palette(
        dark=dark, bg=bg, surface=surface, border=border, text=text, muted=muted,
        field=field, accent=accent, accent_text=readable_on(accent),
        accent_hover=mix(accent, "#ffffff" if not dark else "#000000", 0.15),
        hover=mix(surface, text, 0.08),
        ok=mix(accent, "#3ddc84", 0.6), warn="#e0813c",
    )


# -------------------------------------------------------------------- apply
def apply(root, scale: float = 1.0, dark=None, accent=None) -> Palette:
    """Style every ttk widget class once, and return the palette in use."""
    palette = build_palette(system_dark() if dark is None else dark,
                            accent or system_accent())
    # Tk measures point sizes through its own scaling factor, so tell it the
    # truth about the screen and then ask for plain 10pt text.
    try:
        root.tk.call("tk", "scaling", scale * 96.0 / 72.0)
    except Exception:
        pass
    size = 10
    base = tkfont.nametofont("TkDefaultFont")
    base.configure(family=FONT_FAMILY, size=size)
    for name, extra in (("TkTextFont", {}), ("TkMenuFont", {}), ("TkHeadingFont", {}),
                        ("TkTooltipFont", {}), ("TkIconFont", {})):
        try:
            tkfont.nametofont(name).configure(family=FONT_FAMILY, size=size, **extra)
        except Exception:
            pass
    try:
        tkfont.nametofont("TkFixedFont").configure(family=MONO_FAMILY, size=size - 1)
    except Exception:
        pass

    root.configure(background=palette.bg)
    style = ttk.Style(root)
    style.theme_use("clam")                 # the only built-in theme worth styling

    pad = int(round(6 * scale))
    style.configure(".", background=palette.bg, foreground=palette.text,
                    fieldbackground=palette.field, bordercolor=palette.border,
                    lightcolor=palette.surface, darkcolor=palette.surface,
                    troughcolor=palette.field, arrowcolor=palette.muted,
                    focuscolor=palette.accent, borderwidth=0, relief="flat")

    style.configure("TFrame", background=palette.bg)
    style.configure("Card.TFrame", background=palette.surface)
    style.configure("TLabel", background=palette.bg, foreground=palette.text)
    style.configure("Card.TLabel", background=palette.surface, foreground=palette.text)
    style.configure("Muted.TLabel", background=palette.bg, foreground=palette.muted)
    style.configure("CardMuted.TLabel", background=palette.surface, foreground=palette.muted)
    style.configure("Title.TLabel", background=palette.bg, foreground=palette.text,
                    font=(FONT_FAMILY, size + 5, "bold"))
    style.configure("Section.TLabel", background=palette.surface, foreground=palette.muted,
                    font=(FONT_FAMILY, size - 1))

    style.configure("TButton", background=palette.field, foreground=palette.text,
                    padding=(pad + 4, pad), borderwidth=0, relief="flat",
                    anchor="center")
    style.map("TButton",
              background=[("disabled", mix(palette.bg, palette.field, 0.4)),
                          ("pressed", mix(palette.field, palette.text, 0.18)),
                          ("active", palette.hover)],
              foreground=[("disabled", palette.muted)])

    style.configure("Accent.TButton", background=palette.accent,
                    foreground=palette.accent_text, padding=(pad + 8, pad),
                    font=(FONT_FAMILY, size, "bold"))
    style.map("Accent.TButton",
              background=[("disabled", mix(palette.bg, palette.accent, 0.35)),
                          ("pressed", mix(palette.accent, "#000000", 0.2)),
                          ("active", palette.accent_hover)],
              foreground=[("disabled", mix(palette.bg, palette.accent_text, 0.5))])

    style.configure("Quiet.TButton", background=palette.surface,
                    foreground=palette.muted, padding=(pad, pad - 2))
    style.map("Quiet.TButton",
              background=[("active", palette.hover)],
              foreground=[("active", palette.text)])

    for widget in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(widget, fieldbackground=palette.field, background=palette.field,
                        foreground=palette.text, bordercolor=palette.border,
                        insertcolor=palette.text, padding=pad - 2, arrowcolor=palette.muted,
                        selectbackground=palette.accent,
                        selectforeground=palette.accent_text)
        style.map(widget,
                  fieldbackground=[("readonly", palette.field), ("disabled", palette.bg)],
                  foreground=[("disabled", palette.muted)],
                  bordercolor=[("focus", palette.accent)])
    root.option_add("*TCombobox*Listbox.background", palette.field)
    root.option_add("*TCombobox*Listbox.foreground", palette.text)
    root.option_add("*TCombobox*Listbox.selectBackground", palette.accent)
    root.option_add("*TCombobox*Listbox.selectForeground", palette.accent_text)

    for widget in ("TCheckbutton", "TRadiobutton"):
        style.configure(widget, background=palette.surface, foreground=palette.text,
                        indicatorcolor=palette.field, indicatorbackground=palette.field,
                        indicatorforeground=palette.accent_text,
                        bordercolor=palette.border, padding=(pad - 2, 3),
                        indicatormargin=(0, 0, pad - 2, 0))
        style.map(widget,
                  background=[("active", palette.surface)],
                  indicatorcolor=[("selected", palette.accent),
                                  ("pressed", palette.accent_hover),
                                  ("!selected", palette.field)],
                  indicatorbackground=[("selected", palette.accent),
                                       ("!selected", palette.field)],
                  bordercolor=[("selected", palette.accent),
                               ("!selected", mix(palette.field, palette.text, 0.25))],
                  foreground=[("disabled", palette.muted)])
    style.configure("Plain.TCheckbutton", background=palette.bg)
    style.map("Plain.TCheckbutton", background=[("active", palette.bg)],
              indicatorcolor=[("selected", palette.accent)])
    style.configure("Plain.TRadiobutton", background=palette.bg)
    style.map("Plain.TRadiobutton", background=[("active", palette.bg)],
              indicatorcolor=[("selected", palette.accent)])

    style.configure("Thin.Horizontal.TProgressbar", thickness=int(round(6 * scale)),
                    background=palette.accent, troughcolor=mix(palette.bg, palette.text, 0.10),
                    bordercolor=palette.bg, lightcolor=palette.accent,
                    darkcolor=palette.accent)

    style.configure("Treeview", background=palette.surface, fieldbackground=palette.surface,
                    foreground=palette.text, borderwidth=0, relief="flat",
                    rowheight=int(round(24 * scale)))
    style.map("Treeview",
              background=[("selected", palette.accent)],
              foreground=[("selected", palette.accent_text)])
    style.configure("Treeview.Heading", background=palette.surface,
                    foreground=palette.muted, borderwidth=0, relief="flat",
                    padding=(pad, pad - 3))
    style.map("Treeview.Heading", background=[("active", palette.hover)])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    style.configure("TScrollbar", background=palette.field, troughcolor=palette.bg,
                    bordercolor=palette.bg, arrowcolor=palette.muted, width=int(round(10 * scale)))
    style.map("TScrollbar", background=[("active", palette.hover)])

    style.configure("TSeparator", background=palette.border)
    return palette


def style_text(widget, palette: Palette, scale: float = 1.0) -> None:
    """tk.Text is not a ttk widget, so it has to be painted by hand."""
    widget.configure(
        background=palette.surface, foreground=palette.text,
        insertbackground=palette.text, selectbackground=palette.accent,
        selectforeground=palette.accent_text, relief="flat", borderwidth=0,
        highlightthickness=0, padx=int(round(10 * scale)), pady=int(round(8 * scale)),
        spacing1=1, spacing3=2, wrap="word",
    )


# ------------------------------------------------------------- checkboxes
ICON_FONTS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
GLYPH_CHECKED = "\ue73a"                # CheckboxComposite: filled box with mark
GLYPH_EMPTY = "\ue739"                  # Checkbox: empty box


def icon_font(root) -> str:
    """The Windows icon font, if this machine has one."""
    try:
        families = set(tkfont.families(root))
    except Exception:
        return ""
    for name in ICON_FONTS:
        if name in families:
            return name
    return ""


class Check:
    """A checkbox that looks like this decade.

    clam draws a boxed X for a ticked ttk.Checkbutton, which is the single most
    dated thing in the window, so the indicator here is the same glyph Windows
    itself uses, with the label to its left the way Hebrew reads.
    """

    def __init__(self, parent, text: str, variable, palette: Palette,
                 scale: float = 1.0, surface: bool = True) -> None:
        from tkinter import ttk as _ttk
        self.var = variable
        self.palette = palette
        self._trace = None
        background = palette.surface if surface else palette.bg
        style = "Card.TLabel" if surface else "TLabel"
        self.frame = _ttk.Frame(parent, style="Card.TFrame" if surface else "TFrame")
        family = icon_font(parent)
        size = int(round(11 * scale)) if family else int(round(10 * scale))
        self.box = _ttk.Label(self.frame, style=style,
                              font=(family or FONT_FAMILY, size))
        self.box.pack(side="right", padx=(int(round(6 * scale)), 0))
        self.label = _ttk.Label(self.frame, text=text, style=style)
        self.label.pack(side="right")
        self.glyphs = (GLYPH_EMPTY, GLYPH_CHECKED) if family else ("\u2610", "\u2611")
        for widget in (self.frame, self.box, self.label):
            widget.bind("<Button-1>", self._toggle)
            widget.configure(cursor="hand2")
        self._background = background
        self._redraw()
        # The variable outlives this widget (it belongs to the main window), so
        # the trace has to come off again when the dialog closes - otherwise
        # every reopening leaves another one behind, firing at dead widgets.
        try:
            self._trace = ("write", self.var.trace_add("write", self._on_write))
        except AttributeError:                  # very old Tk
            self._trace = ("w", self.var.trace("w", self._on_write))
        self.frame.bind("<Destroy>", self._unhook, add="+")

    def _on_write(self, *_args) -> None:
        self._redraw()

    def _unhook(self, event=None) -> None:
        if event is not None and event.widget is not self.frame:
            return                              # a child's own Destroy event
        mode, name = self._trace or (None, None)
        self._trace = None
        if not name:
            return
        try:
            self.var.trace_remove(mode, name)
        except AttributeError:                  # very old Tk
            try:
                self.var.trace_vdelete(mode, name)
            except Exception:
                pass
        except Exception:
            pass

    def _toggle(self, _event=None) -> None:
        self.var.set(not bool(self.var.get()))

    def _redraw(self) -> None:
        on = bool(self.var.get())
        try:
            self.box.configure(text=self.glyphs[1] if on else self.glyphs[0],
                               foreground=self.palette.accent if on else self.palette.muted)
            self.label.configure(
                foreground=self.palette.text if on else self.palette.muted)
        except TclError:                        # destroyed before we unhooked
            self._unhook()

    def grid(self, **kwargs):
        self.frame.grid(**kwargs)
        return self

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)
        return self


class Tooltip:
    """A hover bubble. Tk has no such widget, so it is a borderless toplevel."""

    def __init__(self, widget, text_getter, palette: Palette, scale: float = 1.0,
                 delay: int = 450, width: int = 420) -> None:
        self.widget = widget
        self.text_getter = text_getter
        self.palette = palette
        self.delay = delay
        self.width = int(width * scale)
        self.scale = scale
        self._after = None
        self._tip = None
        for event, handler in (("<Enter>", self._schedule), ("<Leave>", self._hide),
                               ("<ButtonPress>", self._hide), ("<Destroy>", self._hide)):
            widget.bind(event, handler, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        try:
            self._after = self.widget.after(self.delay, self._show)
        except Exception:
            self._after = None

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self) -> None:
        self._after = None
        text = self.text_getter() if callable(self.text_getter) else self.text_getter
        if not text or self._tip is not None:
            return
        try:
            import tkinter as tk
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.configure(background=self.palette.border)
            tk.Label(tip, text=text, justify="right", wraplength=self.width,
                     background=self.palette.surface, foreground=self.palette.text,
                     font=(FONT_FAMILY, int(round(9 * self.scale))),
                     padx=int(round(10 * self.scale)), pady=int(round(7 * self.scale)),
                     bd=0, highlightthickness=0).pack(padx=1, pady=1)
            tip.update_idletasks()
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + int(6 * self.scale)
            x = max(0, min(x, tip.winfo_screenwidth() - tip.winfo_width() - 8))
            tip.wm_geometry("+%d+%d" % (x, y))
            self._tip = tip
        except Exception:
            self._tip = None

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


# ------------------------------------------------------------------- icon
def icon_path() -> str:
    """Where the application icon lives, in a source tree or inside an exe."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("icon.ico", "icon.png"):
        candidate = os.path.join(base, "assets", name)
        if os.path.isfile(candidate):
            return candidate
    return ""


def set_window_icon(window) -> bool:
    """Give the window (and the taskbar) the application icon."""
    path = icon_path()
    if not path:
        return False
    try:
        if path.lower().endswith(".ico"):
            window.iconbitmap(default=path)     # also covers later toplevels
        else:
            import tkinter as tk
            image = tk.PhotoImage(file=path)
            window.iconphoto(True, image)
            window._icon_image = image          # keep a reference alive
        return True
    except Exception:
        return False
