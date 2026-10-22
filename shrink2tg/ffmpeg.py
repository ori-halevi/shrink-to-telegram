"""Locating and driving the ffmpeg / ffprobe binaries."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Sequence

_NO_WINDOW = 0
if sys.platform == "win32":
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

# Folders that are worth a look before giving up on auto detection.  Anything
# machine specific belongs in settings.json (ffmpeg_path), not in the source.
_EXTRA_DIRS = [
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\Program Files (x86)\ffmpeg\bin",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "ffmpeg", "bin"),
]


def escape_filter_path(path: str) -> str:
    "Quote a Windows path so it survives the ffmpeg filter graph parser."
    text = path.replace("\\", "/")
    text = text.replace("'", "'\\''")
    return text.replace(":", "\\:")


class FFmpegNotFound(RuntimeError):
    pass


def _exe(name: str) -> str:
    return name + ".exe" if sys.platform == "win32" else name


def _app_dirs() -> List[str]:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    return [base, os.path.join(base, "bin"), os.path.join(base, "ffmpeg", "bin")]


def find_tool(name: str, configured: str = "") -> str:
    """Return a usable path for ``ffmpeg``/``ffprobe`` or an empty string."""
    if configured:
        if os.path.isfile(configured):
            return configured
        # The user may have pointed at the other tool or at the folder.
        folder = configured if os.path.isdir(configured) else os.path.dirname(configured)
        guess = os.path.join(folder, _exe(name))
        if os.path.isfile(guess):
            return guess
    found = shutil.which(name)
    if found:
        return found
    for folder in _app_dirs() + _EXTRA_DIRS:
        guess = os.path.join(folder, _exe(name))
        if os.path.isfile(guess):
            return guess
    return ""


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(args: Sequence[str], timeout: Optional[float] = None) -> Result:
    """Run a command and capture its output (no console window on Windows)."""
    proc = subprocess.run(
        list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
        timeout=timeout,
    )
    return Result(
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


@dataclass
class Progress:
    seconds_done: float = 0.0
    speed: float = 0.0          # encoding speed relative to realtime
    fps: float = 0.0
    out_bytes: int = 0


def run_encode(
    args: Sequence[str],
    on_progress: Optional[Callable[[Progress], None]] = None,
    cancel: Optional[threading.Event] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> Result:
    """Run ffmpeg, streaming ``-progress`` updates back to the caller."""
    cmd = list(args)
    # -progress on stdout keeps parsing trivial and machine readable.
    cmd = cmd[:1] + ["-hide_banner", "-nostdin", "-progress", "pipe:1", "-nostats"] + cmd[1:]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    if cancel is not None and hasattr(cancel, "attach"):
        cancel.attach(proc)              # lets the user pause or stop this run
    progress = Progress()
    stderr_tail: List[str] = []

    def pump_stderr() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            stderr_tail.append(line)
            del stderr_tail[:-60]
            if on_log:
                on_log(line)

    err_thread = threading.Thread(target=pump_stderr, daemon=True)
    err_thread.start()

    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            if cancel is not None and cancel.is_set():
                _terminate(proc)
                break
            line = raw.decode("utf-8", "replace").strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            try:
                if key == "out_time_us" or key == "out_time_ms":
                    # ffmpeg reports microseconds under both spellings.
                    progress.seconds_done = max(0.0, int(value) / 1_000_000)
                elif key == "total_size":
                    progress.out_bytes = int(value)
                elif key == "fps":
                    progress.fps = float(value)
                elif key == "speed":
                    progress.speed = float(value.rstrip("x") or 0)
            except ValueError:
                continue
            if key == "progress" or key == "speed":
                if on_progress:
                    on_progress(progress)
    finally:
        proc.wait()
        err_thread.join(timeout=2)
        if cancel is not None and hasattr(cancel, "detach"):
            cancel.detach(proc)

    if cancel is not None and cancel.is_set():
        return Result(-9, "", "cancelled")
    return Result(proc.returncode, "", "\n".join(stderr_tail))


def _terminate(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


@dataclass
class Toolset:
    ffmpeg: str
    ffprobe: str
    version: str = ""
    encoders: set = field(default_factory=set)
    filters: set = field(default_factory=set)

    def has(self, encoder: str) -> bool:
        return encoder in self.encoders

    def has_filter(self, name: str) -> bool:
        return name in self.filters


def load_toolset(ffmpeg_path: str = "", ffprobe_path: str = "") -> Toolset:
    ffmpeg = find_tool("ffmpeg", ffmpeg_path)
    if not ffmpeg:
        raise FFmpegNotFound("ffmpeg.exe not found")
    ffprobe = find_tool("ffprobe", ffprobe_path or os.path.dirname(ffmpeg))
    if not ffprobe:
        raise FFmpegNotFound("ffprobe.exe not found")
    tools = Toolset(ffmpeg=ffmpeg, ffprobe=ffprobe)
    res = run([ffmpeg, "-hide_banner", "-version"], timeout=30)
    if res.ok:
        tools.version = res.stdout.splitlines()[0] if res.stdout else ""
    res = run([ffmpeg, "-hide_banner", "-encoders"], timeout=30)
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            tools.encoders.add(parts[1])
    res = run([ffmpeg, "-hide_banner", "-filters"], timeout=30)
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            tools.filters.add(parts[1])
    return tools


def probe_hardware(tools: Toolset, encoder: str) -> bool:
    """Encode two synthetic frames to check that a GPU encoder really works."""
    if not tools.has(encoder):
        return False
    null_out = "NUL" if sys.platform == "win32" else "/dev/null"
    res = run(
        [
            tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "color=c=black:s=640x480:r=30",
            "-frames:v", "2", "-c:v", encoder, "-f", "null", null_out,
        ],
        timeout=60,
    )
    return res.ok


# --------------------------------------------------------------- pause/cancel
def _set_process_frozen(pid: int, frozen: bool) -> bool:
    """Freeze or thaw a running process without losing its work."""
    if sys.platform == "win32":
        PROCESS_SUSPEND_RESUME = 0x0800
        try:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ntdll = ctypes.WinDLL("ntdll")
            handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
            if not handle:
                return False
            try:
                call = ntdll.NtSuspendProcess if frozen else ntdll.NtResumeProcess
                return call(handle) == 0
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        import signal
        os.kill(pid, signal.SIGSTOP if frozen else signal.SIGCONT)
        return True
    except Exception:
        return False


class Control:
    """Cancel *and* pause for whatever ffmpeg is running right now.

    Pausing suspends the ffmpeg process itself, so the encode continues from
    exactly where it stopped - nothing is re-done and no output is lost.
    Deliberately mimics :class:`threading.Event` so it can be passed wherever a
    cancel event was expected.
    """

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._paused = False
        self._procs: List[subprocess.Popen] = []
        self._lock = threading.Lock()

    # -- the Event side of the API
    def is_set(self) -> bool:
        return self._cancel.is_set()

    def set(self) -> None:
        self.request_cancel()

    def clear(self) -> None:
        self.resume()
        self._cancel.clear()

    # -- process bookkeeping
    def attach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.append(proc)
            paused = self._paused
        if paused:                       # started while the user is paused
            _set_process_frozen(proc.pid, True)

    def detach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if proc in self._procs:
                self._procs.remove(proc)

    # -- pause / resume
    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> bool:
        with self._lock:
            if self._cancel.is_set():
                return False
            self._paused = True
            procs = list(self._procs)
        for proc in procs:
            if proc.poll() is None:
                _set_process_frozen(proc.pid, True)
        return True

    def resume(self) -> bool:
        with self._lock:
            self._paused = False
            procs = list(self._procs)
        for proc in procs:
            if proc.poll() is None:
                _set_process_frozen(proc.pid, False)
        return True

    def toggle_pause(self) -> bool:
        """Flip the pause state and report whether we are now paused."""
        if self._paused:
            self.resume()
        else:
            self.pause()
        return self._paused

    def request_cancel(self) -> None:
        self._cancel.set()
        self.resume()                    # a frozen process can never notice it
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            if proc.poll() is None:
                _terminate(proc)
