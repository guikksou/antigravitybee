#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Platform Abstraction Layer for Antigravity Bee Companion.
Cleanly separates Windows (Win32) and Linux (X11/Wayland/POSIX) specifics:
- DPI awareness
- Frameless Always-on-Top & Transparency setup
- Non-blocking audio chimes
- Detached background process spawning
- PID liveness check & termination
"""

from __future__ import annotations

import atexit
import ctypes
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import tkinter as tk

TRANSPARENT_KEY = "#010203"
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")


def is_wayland_session() -> bool:
    """
    Return True when running inside a Linux Wayland session (e.g. Hyprland, Sway,
    GNOME/KDE Wayland), unless explicitly forced to Tkinter via AGY_BEE_BACKEND=tk.
    Always returns False on Windows and Linux X11.
    """
    if not IS_LINUX:
        return False
    backend = os.environ.get("AGY_BEE_BACKEND", "").strip().lower()
    if backend == "tk":
        return False
    if backend == "wayland":
        return True
    return bool(
        os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland"
        or os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        or os.environ.get("SWAYSOCK")
    )

_SINGLETON_HANDLE = None
_KERNEL32 = None
_X11_LIB = None
_XEXT_LIB = None
_X11_DISPLAY = None
_X11_INIT_DONE = False
_X11_TOPLEVEL_CACHE = {}

_WRAPPER_NAMES = {
    "python",
    "python.exe",
    "python3",
    "pythonw",
    "pythonw.exe",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "conhost",
    "conhost.exe",
    "sh",
    "sh.exe",
    "bash",
    "bash.exe",
    "dash",
    "zsh",
    "fish",
    "nu",
    "uv",
    "uv.exe",
    "env",
}
_WRAPPER_PREFIXES = (
    "python",
    "py",
    "pwsh",
    "powershell",
    "cmd",
    "sh",
    "bash",
    "zsh",
    "dash",
    "fish",
    "nu",
    "conhost",
    "uv",
    "env",
)


def _is_wrapper_process(name: str) -> bool:
    """Return True if process name is a shell, interpreter, or launcher wrapper."""
    return name in _WRAPPER_NAMES or name.startswith(_WRAPPER_PREFIXES)


if IS_WINDOWS:
    from ctypes import wintypes

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]


def _get_kernel32():
    """Return kernel32 WinDLL with use_last_error=True and 64-bit-safe argtypes/restype."""
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE

    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE

    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL

    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL

    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wintypes.BOOL

    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL

    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    _KERNEL32 = k32
    return _KERNEL32


class _XRectangle(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_short),
        ("y", ctypes.c_short),
        ("width", ctypes.c_ushort),
        ("height", ctypes.c_ushort),
    ]


def _ensure_x11_shape():
    """Lazily initialize libX11 and libXext for XShape non-rectangular window masks on Linux."""
    global _X11_LIB, _XEXT_LIB, _X11_DISPLAY, _X11_INIT_DONE
    if _X11_INIT_DONE:
        return _X11_DISPLAY is not None and _XEXT_LIB is not None
    _X11_INIT_DONE = True
    if not IS_LINUX:
        return False
    try:
        from ctypes.util import find_library

        x11_name = find_library("X11") or "libX11.so.6"
        xext_name = find_library("Xext") or "libXext.so.6"
        _X11_LIB = ctypes.cdll.LoadLibrary(x11_name)
        _XEXT_LIB = ctypes.cdll.LoadLibrary(xext_name)

        _X11_LIB.XOpenDisplay.argtypes = [ctypes.c_char_p]
        _X11_LIB.XOpenDisplay.restype = ctypes.c_void_p
        _X11_LIB.XFlush.argtypes = [ctypes.c_void_p]
        _X11_LIB.XFlush.restype = ctypes.c_int
        _X11_LIB.XQueryTree.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        _X11_LIB.XQueryTree.restype = ctypes.c_int
        _X11_LIB.XFree.argtypes = [ctypes.c_void_p]
        _X11_LIB.XFree.restype = ctypes.c_int
        _X11_LIB.XCloseDisplay.argtypes = [ctypes.c_void_p]
        _X11_LIB.XCloseDisplay.restype = ctypes.c_int

        _XEXT_LIB.XShapeCombineRectangles.argtypes = [
            ctypes.c_void_p,  # Display*
            ctypes.c_ulong,   # XID (Window)
            ctypes.c_int,     # dest_kind (ShapeBounding=0, ShapeInput=2)
            ctypes.c_int,     # x_off
            ctypes.c_int,     # y_off
            ctypes.POINTER(_XRectangle),
            ctypes.c_int,     # n_rects
            ctypes.c_int,     # op (ShapeSet=0)
            ctypes.c_int,     # ordering (Unsorted=0)
        ]
        _XEXT_LIB.XShapeCombineRectangles.restype = None

        _X11_DISPLAY = _X11_LIB.XOpenDisplay(None)
        if _X11_DISPLAY:
            atexit.register(_close_x11_display)
        return bool(_X11_DISPLAY)
    except Exception:
        _X11_DISPLAY = None
        return False


def _close_x11_display():
    """Close the cached X11 Display connection cleanly on process exit."""
    global _X11_DISPLAY
    if _X11_LIB is not None and _X11_DISPLAY:
        try:
            _X11_LIB.XCloseDisplay(_X11_DISPLAY)
        except Exception:
            pass
        _X11_DISPLAY = None


def _get_x11_toplevel_and_inner(root: tk.Tk) -> list[int]:
    """
    On Linux/Tkinter, root.winfo_id() returns the inner wrapper XID (e.g. 0x2a00009),
    while the actual X11 TopLevel window managed by the compositor/WM is its parent XID (e.g. 0x2a0000a).
    Returns both XIDs so XShape applies cleanly to the outer frame and inner wrapper.
    """
    inner_xid = root.winfo_id()
    if not inner_xid:
        return []
    if inner_xid in _X11_TOPLEVEL_CACHE:
        return _X11_TOPLEVEL_CACHE[inner_xid]
    targets = [inner_xid]
    try:
        wm_frame_str = root.wm_frame()
        if wm_frame_str and wm_frame_str != "0x0":
            outer_xid = int(wm_frame_str, 16)
            if outer_xid and outer_xid != inner_xid:
                targets.append(outer_xid)
    except Exception:
        pass
    if len(targets) == 1 and _X11_LIB and _X11_DISPLAY:
        try:
            root_ret = ctypes.c_ulong()
            parent_ret = ctypes.c_ulong()
            children_ret = ctypes.POINTER(ctypes.c_ulong)()
            nchildren_ret = ctypes.c_uint()
            if _X11_LIB.XQueryTree(
                _X11_DISPLAY,
                inner_xid,
                ctypes.byref(root_ret),
                ctypes.byref(parent_ret),
                ctypes.byref(children_ret),
                ctypes.byref(nchildren_ret),
            ):
                if children_ret:
                    _X11_LIB.XFree(children_ret)
                if parent_ret.value and parent_ret.value != root_ret.value:
                    targets.append(parent_ret.value)
        except Exception:
            pass
    if len(targets) > 1:
        _X11_TOPLEVEL_CACHE[inner_xid] = targets
    return targets


def apply_window_shape_rects(root: tk.Tk, rects: list[tuple[int, int, int, int]]) -> bool:
    """
    On Linux (X11/XWayland), apply an exact pixel-art non-rectangular bounding mask
    to both the outer TopLevel window and inner Tk wrapper so the background is 100%
    transparent without any black box or unredrawn artifacts.
    Returns True if the XShape mask was applied, False otherwise.
    No-op on Windows (which uses -transparentcolor natively).
    """
    if not IS_LINUX or not _ensure_x11_shape():
        return False
    try:
        root.update_idletasks()
        if not root.winfo_ismapped():
            return False
        targets = _get_x11_toplevel_and_inner(root)
        if not targets:
            return False
        n = len(rects)
        if n == 0:
            arr = (_XRectangle * 1)((0, 0, 1, 1))
            n = 1
        else:
            arr = (_XRectangle * n)(*rects)
        # ShapeBounding = 0, ShapeSet = 0, Unsorted = 0
        for xid in targets:
            _XEXT_LIB.XShapeCombineRectangles(_X11_DISPLAY, xid, 0, 0, 0, arr, n, 0, 0)
        _X11_LIB.XFlush(_X11_DISPLAY)
        return True
    except Exception:
        return False


def acquire_single_instance_lock(lock_path: Path) -> bool:
    """
    Acquire an OS-level kernel lock (Win32 Named Mutex on Windows, fcntl flock on Linux).
    Returns True if this process is the sole active instance, False if another bee is already running.
    """
    global _SINGLETON_HANDLE
    if IS_WINDOWS:
        try:
            kernel32 = _get_kernel32()
            mutex_name = "Local\\AntigravityBeeCompanionSingletonMutex"
            ctypes.set_last_error(0)
            handle = kernel32.CreateMutexW(None, False, mutex_name)
            last_err = ctypes.get_last_error()
            if not handle:
                return False
            if last_err == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            _SINGLETON_HANDLE = handle
            return True
        except Exception:
            return False
    else:
        try:
            import fcntl
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fp = open(lock_path, "w")
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _SINGLETON_HANDLE = fp
            return True
        except Exception:
            return False


def init_dpi_awareness():
    """Enable crisp high-DPI rendering before Tk root creation."""
    if not IS_WINDOWS:
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (HANDLE)-4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        for level in (2, 1):
            if shcore.SetProcessDpiAwareness(level) == 0:
                return
    except Exception:
        pass
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDPIAware.argtypes = []
        user32.SetProcessDPIAware.restype = wintypes.BOOL
        user32.SetProcessDPIAware()
    except Exception:
        pass


def configure_overlay_window(root: tk.Tk, transparent_key: str = TRANSPARENT_KEY):
    """Configure frameless, always-on-top, transparent window for current OS."""
    root.overrideredirect(True)
    root.wm_attributes("-topmost", True)

    if IS_WINDOWS:
        root.wm_attributes("-transparentcolor", transparent_key)
    # On Linux, overrideredirect(True) + -topmost avoids EWMH dock struts and
    # wait_visibility() hangs on tiling/headless WMs before geometry/pack.


def set_window_opacity(root: tk.Tk, alpha: float):
    """Safely set window opacity across Windows and Linux."""
    try:
        root.wm_attributes("-alpha", alpha)
    except Exception:
        pass


def is_pid_alive(pid: int) -> bool:
    """Check if a process ID is currently active."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = _get_kernel32()
            ctypes.set_last_error(0)
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                try:
                    exit_code = wintypes.DWORD()
                    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                    return bool(ok) and exit_code.value == 259  # STILL_ACTIVE
                finally:
                    kernel32.CloseHandle(handle)
            # ERROR_ACCESS_DENIED (5) means the process exists at a higher integrity level
            return ctypes.get_last_error() == 5
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False


def get_antigravity_host_pid() -> int:
    """
    Walk up the process tree from the current hook/CLI process past shell and Python
    wrappers to find the actual Antigravity host process PID (Windows & Linux).
    """
    if IS_WINDOWS:
        try:
            kernel32 = _get_kernel32()
            snap = kernel32.CreateToolhelp32Snapshot(2, 0)
            if not snap or snap == _INVALID_HANDLE_VALUE:
                return os.getppid()
            procs = {}
            try:
                pe = _PROCESSENTRY32W()
                pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
                if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
                    while True:
                        procs[pe.th32ProcessID] = (
                            pe.th32ParentProcessID,
                            pe.szExeFile.lower(),
                        )
                        if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                            break
            finally:
                kernel32.CloseHandle(snap)

            cur = os.getppid()
            visited = set()
            while cur in procs and cur not in visited:
                visited.add(cur)
                ppid, name = procs[cur]
                if not _is_wrapper_process(name):
                    return cur
                if ppid <= 0 or ppid == cur:
                    break
                cur = ppid
            return cur if cur > 0 else os.getppid()
        except Exception:
            return os.getppid()
    else:
        try:
            cur = os.getppid()
            visited = set()
            while cur > 1 and cur not in visited:
                visited.add(cur)
                comm_path = Path(f"/proc/{cur}/comm")
                stat_path = Path(f"/proc/{cur}/stat")
                if not comm_path.exists() or not stat_path.exists():
                    break
                name = comm_path.read_text(encoding="utf-8").strip().lower()
                if not _is_wrapper_process(name):
                    return cur
                stat_txt = stat_path.read_text(encoding="utf-8")
                after_comm = stat_txt.rsplit(")", 1)[-1].split()
                ppid = int(after_comm[1])
                if ppid <= 1 or ppid == cur:
                    break
                cur = ppid
            return cur
        except Exception:
            return os.getppid()


def terminate_pid(pid: int):
    """Terminate an existing process ID cleanly across platforms."""
    if pid <= 0 or pid == os.getpid():
        return
    if IS_WINDOWS:
        try:
            PROCESS_TERMINATE = 0x0001
            kernel32 = _get_kernel32()
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                try:
                    kernel32.TerminateProcess(handle, 0)
                finally:
                    kernel32.CloseHandle(handle)
        except Exception:
            pass
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def spawn_detached_widget(script_path: Path) -> int:
    """Spawn bee_widget.py as an independent background GUI process."""
    py_exe = Path(sys.executable)
    if IS_WINDOWS:
        pythonw = py_exe.parent / "pythonw.exe"
        exe = str(pythonw if pythonw.exists() else py_exe)
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        proc = subprocess.Popen(
            [exe, str(script_path)],
            cwd=str(script_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
        return proc.pid
    else:
        # Linux / macOS POSIX detached session
        env = os.environ.copy()
        for var in (
            "WAYLAND_DISPLAY",
            "HYPRLAND_INSTANCE_SIGNATURE",
            "SWAYSOCK",
            "XDG_RUNTIME_DIR",
            "XDG_SESSION_TYPE",
            "DISPLAY",
        ):
            val = os.environ.get(var)
            if val is not None and var not in env:
                env[var] = val
        if "DISPLAY" not in env or not env.get("DISPLAY"):
            if Path("/tmp/.X11-unix/X0").exists():
                env["DISPLAY"] = ":0"
            elif Path("/tmp/.X11-unix/X1").exists():
                env["DISPLAY"] = ":1"
            elif not is_wayland_session():
                env["DISPLAY"] = ":0"
        proc = subprocess.Popen(
            [str(py_exe), str(script_path)],
            cwd=str(script_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
        return proc.pid


def play_platform_chime(kind: str = "", enabled: bool = True):
    """Silent by design (sound removed per user preference)."""
    return
