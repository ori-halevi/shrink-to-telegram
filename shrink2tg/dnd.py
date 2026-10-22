# -*- coding: utf-8 -*-
"""Dragging files straight into the window.

Tkinter has no drag and drop of its own, and pulling in tkinterdnd2 would cost
us the "copy the folder and run it anywhere" portability.  On Windows the whole
feature is three Win32 calls, so we subclass the window procedure of the
toplevel and listen for WM_DROPFILES ourselves.
"""
from __future__ import annotations

import ctypes
import queue
import sys
from ctypes import wintypes
from typing import Callable, List, Optional

WM_DROPFILES = 0x0233
GWL_WNDPROC = -4

# The installed procedures must stay referenced or Python would collect them
# while Windows still holds the pointer.
_installed: List[object] = []


def enable_file_drop(widget, callback: Callable[[List[str]], None]) -> bool:
    """Let the user drop files on ``widget``'s window. True when it worked."""
    if sys.platform != "win32":
        return False
    try:
        return _install(widget, callback)
    except Exception:                    # a missing API must never break the app
        return False


def _toplevel_hwnd(widget, user32) -> Optional[int]:
    top = widget.winfo_toplevel()
    top.update_idletasks()               # the frame exists only once mapped
    try:
        handle = int(top.wm_frame(), 16)
        if handle:
            return handle
    except (ValueError, AttributeError, ctypes.ArgumentError):
        pass
    parent = user32.GetParent(wintypes.HWND(top.winfo_id()))
    return int(parent) if parent else None


def _install(widget, callback: Callable[[List[str]], None]) -> bool:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    user32.GetParent.restype = wintypes.HWND
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.CallWindowProcW.restype = LRESULT
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM]
    shell32.DragAcceptFiles.restype = None
    shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                       wintypes.LPWSTR, wintypes.UINT]
    shell32.DragFinish.restype = None
    shell32.DragFinish.argtypes = [ctypes.c_void_p]

    # 64 bit Windows needs the ...Ptr variant; 32 bit only has the short one.
    set_proc = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    set_proc.restype = LRESULT
    set_proc.argtypes = [wintypes.HWND, ctypes.c_int, WNDPROC]
    get_proc = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    get_proc.restype = LRESULT
    get_proc.argtypes = [wintypes.HWND, ctypes.c_int]

    hwnd = _toplevel_hwnd(widget, user32)
    if not hwnd:
        return False

    def dropped_files(drop_handle) -> List[str]:
        count = shell32.DragQueryFileW(drop_handle, 0xFFFFFFFF, None, 0)
        paths: List[str] = []
        for index in range(count):
            length = shell32.DragQueryFileW(drop_handle, index, None, 0)
            buffer = ctypes.create_unicode_buffer(length + 1)
            shell32.DragQueryFileW(drop_handle, index, buffer, length + 1)
            if buffer.value:
                paths.append(buffer.value)
        return paths

    pending: "queue.Queue[List[str]]" = queue.Queue()

    def drain() -> None:
        """Tk's own turn: here calling back into the widgets is safe."""
        try:
            while True:
                try:
                    paths = pending.get_nowait()
                except queue.Empty:
                    break
                callback(paths)
        finally:
            try:
                widget.after(120, drain)
            except Exception:            # the window closed under us
                pass

    # Captured before the hook is installed: between SetWindowLongPtr and the
    # assignment below, Windows can already deliver a message, and answering it
    # with DefWindowProcW instead of Tk's own procedure loses it.
    previous = ctypes.c_void_p(
        user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_WNDPROC)
        if hasattr(user32, "GetWindowLongPtrW")
        else user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_WNDPROC))

    def handler(handle, message, wparam, lparam):
        if message == WM_DROPFILES:
            drop = ctypes.c_void_p(wparam)
            try:
                paths = dropped_files(drop)
            finally:
                shell32.DragFinish(drop)
            if paths:
                # Only a queue here: touching Tk from inside the window
                # procedure re-enters the interpreter while mainloop has the
                # GIL released, which kills the process outright.
                pending.put(paths)
            return 0
        if previous is None:                 # a message during installation
            return user32.DefWindowProcW(handle, message, wparam, lparam)
        return user32.CallWindowProcW(previous, handle, message, wparam, lparam)

    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    proc = WNDPROC(handler)
    # Keep the trampoline alive *before* Windows is given its address.
    _installed.append((proc, hwnd))
    older = set_proc(wintypes.HWND(hwnd), GWL_WNDPROC, proc)
    if not older:
        _installed.pop()
        return False
    previous = ctypes.c_void_p(older)       # authoritative value
    shell32.DragAcceptFiles(wintypes.HWND(hwnd), True)
    widget.after(120, drain)
    return True
