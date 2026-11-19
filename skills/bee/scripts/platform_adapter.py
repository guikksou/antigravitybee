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

import ctypes
import os
import signal
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path

TRANSPARENT_KEY = "#010203"
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

_SINGLETON_HANDLE = None


def acquire_single_instance_lock(lock_path: Path) -> bool:
    """
    Acquire an OS-level kernel lock (Win32 Named Mutex on Windows, fcntl flock on Linux).
    Returns True if this process is the sole active instance, False if another bee is already running.
    """
    global _SINGLETON_HANDLE
    if IS_WINDOWS:
        try:
            mutex_name = "Local\\AntigravityBeeCompanionSingletonMutex"
            handle = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
            last_err = ctypes.windll.kernel32.GetLastError()
            if not handle or last_err == 183:  # ERROR_ALREADY_EXISTS
                return False
            _SINGLETON_HANDLE = handle
            return True
        except Exception:
            return True
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

if IS_WINDOWS:
    try:
        import winsound
        HAS_WINSOUND = True
    except ImportError:
        HAS_WINSOUND = False
else:
    HAS_WINSOUND = False


def init_dpi_awareness():
    """Enable crisp high-DPI rendering before Tk root creation."""
    if IS_WINDOWS:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def configure_overlay_window(root: tk.Tk, transparent_key: str = TRANSPARENT_KEY):
    """Configure frameless, always-on-top, transparent window for current OS."""
    root.overrideredirect(True)
    root.wm_attributes("-topmost", True)

    if IS_WINDOWS:
        root.wm_attributes("-transparentcolor", transparent_key)
    elif IS_LINUX:
        # On Linux X11/Wayland compositors, set dock/utility hint and wait visibility
        try:
            root.wm_attributes("-type", "dock")
        except Exception:
            pass
        try:
            root.wait_visibility(root)
        except Exception:
            pass


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
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return exit_code.value == 259  # STILL_ACTIVE
        # ERROR_ACCESS_DENIED (5) means the process exists at a higher integrity level
        return ctypes.windll.kernel32.GetLastError() == 5
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
    wrappers = {
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
    }
    if IS_WINDOWS:
        try:
            from ctypes import wintypes

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            snap = ctypes.windll.kernel32.CreateToolhelp32Snapshot(2, 0)
            if snap == -1:
                return os.getppid()
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            procs = {}
            if ctypes.windll.kernel32.Process32FirstW(snap, ctypes.byref(pe)):
                while True:
                    procs[pe.th32ProcessID] = (pe.th32ParentProcessID, pe.szExeFile.lower())
                    if not ctypes.windll.kernel32.Process32NextW(snap, ctypes.byref(pe)):
                        break
            ctypes.windll.kernel32.CloseHandle(snap)

            cur = os.getpid()
            visited = set()
            while cur in procs and cur not in visited:
                visited.add(cur)
                ppid, name = procs[cur]
                if name not in wrappers:
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
                if name not in wrappers:
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
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                ctypes.windll.kernel32.TerminateProcess(handle, 0)
                ctypes.windll.kernel32.CloseHandle(handle)
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
        if "DISPLAY" not in env and "WAYLAND_DISPLAY" not in env:
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


def play_platform_chime(kind: str, enabled: bool = True):
    """Silent by design (sound removed per user preference)."""
    return
