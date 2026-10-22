# -*- coding: utf-8 -*-
"""Making mixed Hebrew/English log lines read the way they should.

Tk has no bidirectional text engine, so a line is laid out exactly as we hand
it over.  Two things therefore happen here: a line that contains Hebrew is
marked as an RTL paragraph (right aligned, opened with a RIGHT-TO-LEFT MARK so
renderers that *do* understand bidi agree with us), and leading punctuation -
bullets, dashes - is moved to the other end so the first visible character of
the line really is a Hebrew letter.
"""
from __future__ import annotations

RLM = "\u200f"                    # RIGHT-TO-LEFT MARK
LRM = "\u200e"                    # LEFT-TO-RIGHT MARK
LEADING_SYMBOLS = " \t.,:;-\u2013\u2014*\u00b7\u2022>#|"

AUTO, RTL, LTR = "auto", "rtl", "ltr"
MODES = (AUTO, RTL, LTR)


def is_hebrew_char(char: str) -> bool:
    code = ord(char)
    return 0x0590 <= code <= 0x05FF or 0xFB1D <= code <= 0xFB4F


def has_hebrew(text: str) -> bool:
    return any(is_hebrew_char(char) for char in text)


def direction_of(text: str, mode: str = AUTO) -> str:
    """Which way a single line should be laid out."""
    if mode in (RTL, LTR):
        return mode
    return RTL if has_hebrew(text) else LTR


def as_rtl(line: str) -> str:
    """Reshape a line so it opens with Hebrew and is marked right-to-left."""
    core = line.strip()
    if not core:
        return line
    prefix = ""
    while core and core[0] in LEADING_SYMBOLS:
        prefix += core[0]
        core = core[1:]
    prefix = prefix.strip()
    if prefix and core:
        core = "%s %s" % (core, prefix)     # the bullet belongs on the far side
    return RLM + core


def as_ltr(line: str) -> str:
    core = line.strip()
    if not core:
        return line
    return LRM + core if has_hebrew(core) else core


def as_label(text: str) -> str:
    """Interface text: mark a Hebrew label as RTL so its words keep their order.

    Tk reorders each script run but takes the paragraph direction from the
    first strong character, so "\u05d1\u05d3\u05d9\u05e7\u05ea VMAF" would come out with
    the Hebrew on the wrong side. One invisible mark fixes it.
    """
    return RLM + text if has_hebrew(text) else text


def render(line: str, mode: str = AUTO) -> "tuple[str, str]":
    """Return ``(text, direction)`` ready to be written into the log."""
    direction = direction_of(line, mode)
    if direction == RTL:
        return as_rtl(line), RTL
    return as_ltr(line), LTR
