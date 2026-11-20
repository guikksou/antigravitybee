#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
State, Config & Live Transcript Synchronization for Antigravity Bee Companion.
Shared across bee_widget.py, bee_hook.py, and bee_ctl.py.
"""

import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from platform_adapter import is_pid_alive, spawn_detached_widget

IS_WINDOWS = sys.platform == "win32"


def _is_safe_dir(path: Path, check_uid: bool = True) -> bool:
    """Return True if path does not exist or is a real directory owned by the current user."""
    try:
        if path.is_symlink():
            return False
        if path.exists():
            st = path.lstat()
            if not stat.S_ISDIR(st.st_mode):
                return False
            if check_uid and not IS_WINDOWS and hasattr(os, "getuid") and st.st_uid != os.getuid():
                return False
        return True
    except OSError:
        return False


def _prepare_secure_dir(path: Path) -> bool:
    """Create a private 0700 directory, refusing symlinks or foreign ownership."""
    try:
        if path.is_symlink():
            return False
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not _is_safe_dir(path):
            return False
        if not IS_WINDOWS:
            os.chmod(path, 0o700)
        return os.access(path, os.W_OK | os.X_OK)
    except OSError:
        return False


def _resolve_state_dir() -> Path:
    """Resolve a per-user private runtime directory for ephemeral IPC, PID, and lock files."""
    if IS_WINDOWS:
        win_dir = Path(tempfile.gettempdir()) / "agy_bee"
        _prepare_secure_dir(win_dir)
        return win_dir

    uid = os.getuid() if hasattr(os, "getuid") else 0
    xdg_rt = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg_rt:
        rt_base = Path(xdg_rt)
        if rt_base.is_dir() and not rt_base.is_symlink() and os.access(rt_base, os.W_OK | os.X_OK):
            cand = rt_base / "agy_bee"
            if _prepare_secure_dir(cand):
                return cand

    tmp_cand = Path(tempfile.gettempdir()) / f"agy_bee_{uid}"
    if _prepare_secure_dir(tmp_cand):
        return tmp_cand

    cache_cand = Path.home() / ".cache" / f"agy_bee_{uid}"
    if _prepare_secure_dir(cache_cand):
        return cache_cand

    raise RuntimeError(f"Unable to initialize secure runtime state directory (checked {tmp_cand})")


def _is_dir_writable(path: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        path.mkdir(parents=True, exist_ok=True)
        if not _is_safe_dir(path):
            return False
        probe = path / f".probe_{os.getpid()}_{time.monotonic_ns()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _resolve_config_dir(fallback: Path) -> Path:
    """Resolve persistent user config directory, falling back to STATE_DIR if unwritable."""
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    candidate = base / "antigravity-bee"
    if _is_dir_writable(candidate):
        return candidate
    return fallback


STATE_DIR = _resolve_state_dir()
STATE_FILE = STATE_DIR / "bee_state.json"
PID_FILE = STATE_DIR / "bee_pid.txt"

CONFIG_DIR = _resolve_config_dir(STATE_DIR)
CONFIG_FILE = CONFIG_DIR / "bee_config.json"

WIDGET_SCRIPT = Path(__file__).resolve().parent / "bee_widget.py"

DEFAULT_CONFIG = {
    "auto_launch": True,
    "ghost": False,
    "size": "medium",
}


def _ensure_state_dir():
    if STATE_DIR.is_symlink():
        raise PermissionError(f"Refusing symlinked STATE_DIR: {STATE_DIR}")
    if not STATE_DIR.exists():
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _is_safe_dir(STATE_DIR):
        raise PermissionError(f"Unsafe STATE_DIR: {STATE_DIR}")
    if not IS_WINDOWS:
        os.chmod(STATE_DIR, 0o700)


def _atomic_write_text(target: Path, content: str, secure_state: bool = False):
    """Atomically write content via a unique temp file + os.replace with Windows retry."""
    if secure_state:
        _ensure_state_dir()
    else:
        if target.parent.is_symlink():
            raise PermissionError(f"Refusing symlinked parent directory: {target.parent}")
        target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_symlink():
        raise PermissionError(f"Refusing to overwrite symlink: {target}")

    tmp_file = target.parent / f".tmp_{os.getpid()}_{time.monotonic_ns()}"
    try:
        tmp_file.write_text(content, encoding="utf-8")
        for attempt in range(3):
            try:
                os.replace(tmp_file, target)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except OSError:
                pass


def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists() and not CONFIG_FILE.is_symlink():
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict):
    try:
        _atomic_write_text(CONFIG_FILE, json.dumps(cfg, indent=2), secure_state=False)
    except Exception:
        pass


def write_state(
    mode: str,
    detail: str = "",
    swarm_active: bool | None = None,
    parent_conv_id: str | None = None,
    host_pid: int | None = None,
    transcript_path: str | None = None,
):
    try:
        _ensure_state_dir()
        prev = read_state()
        data = {
            "mode": mode,
            "detail": detail,
            "timestamp": time.time(),
            "swarm_active": prev.get("swarm_active", False) if swarm_active is None else swarm_active,
            "parent_conv_id": prev.get("parent_conv_id", "") if parent_conv_id is None else parent_conv_id,
            "host_pid": prev.get("host_pid", 0) if host_pid is None else host_pid,
            "transcript_path": prev.get("transcript_path", "") if transcript_path is None else transcript_path,
        }
        _atomic_write_text(STATE_FILE, json.dumps(data, ensure_ascii=False), secure_state=True)
    except Exception:
        pass


def read_state() -> dict:
    if STATE_FILE.exists() and not STATE_FILE.is_symlink():
        try:
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass
    return {}


def is_bee_running() -> tuple[bool, int]:
    if not PID_FILE.exists() or PID_FILE.is_symlink():
        return False, 0
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        if is_pid_alive(pid):
            return True, pid
    except Exception:
        pass
    return False, 0


def ensure_bee_running(force: bool = False) -> tuple[bool, int]:
    """
    Ensure the bee widget is running.
    - If force=True (e.g. /bee command), launches even if auto_launch was disabled.
    - If force=False (e.g. lifecycle hook), only launches if auto_launch is True.
    """
    running, pid = is_bee_running()
    if running:
        return True, pid

    cfg = load_config()
    if not force and not cfg.get("auto_launch", True):
        return False, 0

    # Prevent rapid concurrent hook invocations from double-spawning while pythonw boots
    spawn_lock = STATE_DIR / "spawn.stamp"
    now = time.time()
    if not force and spawn_lock.exists() and not spawn_lock.is_symlink():
        try:
            if (now - spawn_lock.stat().st_mtime) < 4.0:
                return True, 0
        except Exception:
            pass
    try:
        _ensure_state_dir()
        if not spawn_lock.is_symlink():
            spawn_lock.write_text(str(now), encoding="utf-8")
        # Clear stale PID file before spawning; the child writes PID_FILE after lock acquisition
        if PID_FILE.exists() and not PID_FILE.is_symlink():
            PID_FILE.unlink()
    except Exception:
        pass

    new_pid = spawn_detached_widget(WIDGET_SCRIPT)
    return (new_pid > 0), max(0, new_pid)


class TranscriptWatcher:
    """Watches active Antigravity transcript.jsonl files across CLI, IDE, and App."""

    _TAIL_CHUNKS = (8192, 65536, 262144, 1048576, 4194304)

    def __init__(self):
        self.last_path: Path | None = None
        self.last_mtime: float = 0.0
        self.last_mtime_ns: int = 0
        self.last_scan: float = -10.0
        self.last_step: int = -1
        self.swarm_parent_path: Path | None = None

    @staticmethod
    def _clean_arg(val) -> str:
        if val is None:
            return ""
        s = str(val).strip()
        if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
            s = s[1:-1]
        return s.replace('\\"', '"').replace("\\\\", "\\")

    def _read_last_json_obj(self, path: Path, file_size: int) -> dict | None:
        """Read and parse the last complete JSONL line, expanding tail buffer for >8KB lines."""
        if file_size <= 0:
            return None
        with open(path, "rb") as f:
            for chunk_size in self._TAIL_CHUNKS:
                offset = max(0, file_size - chunk_size)
                f.seek(offset)
                raw = f.read()
                stripped = raw.rstrip(b"\r\n\t ")
                if not stripped:
                    return None
                nl_idx = stripped.rfind(b"\n")
                if offset > 0 and nl_idx == -1:
                    # Last line starts before our seek offset; expand buffer unless at full file
                    if chunk_size >= file_size:
                        break
                    continue
                last_line_bytes = stripped[nl_idx + 1 :] if nl_idx != -1 else stripped
                last_line = last_line_bytes.decode("utf-8", errors="replace").strip()
                if not last_line:
                    return None
                try:
                    obj = json.loads(last_line)
                    if isinstance(obj, dict):
                        return obj
                    return None
                except json.JSONDecodeError:
                    if offset == 0 or chunk_size >= file_size:
                        return None
        return None

    def poll(self, now: float = 0.0) -> tuple[str, str] | None:
        st_data = read_state()
        if not st_data.get("swarm_active", False):
            self.swarm_parent_path = None

        state_tp = st_data.get("transcript_path", "")
        resolved_tp: Path | None = None
        if state_tp:
            try:
                cand = Path(state_tp)
                if cand.exists() and cand.is_file():
                    resolved_tp = cand
            except Exception:
                resolved_tp = None

        if resolved_tp is not None:
            if resolved_tp != self.last_path:
                self.last_path = resolved_tp
                self.last_mtime = 0.0
                self.last_mtime_ns = 0
                self.last_step = -1
        else:
            mono_now = time.monotonic()
            if self.last_path is None or (mono_now - self.last_scan >= 5.0):
                self.last_scan = mono_now
                gemini_dir = Path.home() / ".gemini"
                newest_path = None
                newest_mtime = 0.0
                try:
                    for prod in ("antigravity-cli", "antigravity", "antigravity-ide"):
                        brain_dir = gemini_dir / prod / "brain"
                        if not brain_dir.exists():
                            continue
                        for conv_dir in brain_dir.iterdir():
                            t_file = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
                            if t_file.exists():
                                mt = t_file.stat().st_mtime
                                if mt > newest_mtime:
                                    newest_mtime = mt
                                    newest_path = t_file
                except Exception:
                    pass
                if newest_path is not None and newest_path != self.last_path:
                    self.last_path = newest_path
                    self.last_mtime = 0.0
                    self.last_mtime_ns = 0
                    self.last_step = -1

        if not self.last_path or not self.last_path.exists():
            return None

        try:
            st = self.last_path.stat()
            if st.st_mtime_ns <= self.last_mtime_ns and st.st_mtime <= self.last_mtime:
                return None

            last_obj = self._read_last_json_obj(self.last_path, st.st_size)
            if last_obj is None:
                return None

            self.last_mtime = st.st_mtime
            self.last_mtime_ns = st.st_mtime_ns

            step_idx = last_obj.get("step_index", -1)
            if step_idx == self.last_step:
                return None
            self.last_step = step_idx

            step_type = last_obj.get("type", "")
            status = last_obj.get("status", "")
            tool_calls = last_obj.get("tool_calls") or []

            swarm_active = bool(st_data.get("swarm_active", False)) or (
                self.swarm_parent_path is not None and self.last_path != self.swarm_parent_path
            )

            def wrap_result(m: str, d: str) -> tuple[str, str]:
                if swarm_active and m not in ("needs_input", "error", "done"):
                    return ("swarm", d if d.startswith("Swarm") else f"Swarm: {d}")
                return (m, d)

            if status == "ERROR":
                self.swarm_parent_path = None
                return ("error", "Error encountered")

            if step_type == "USER_INPUT":
                return wrap_result("thinking", "Thinking...")

            if step_type == "PLANNER_RESPONSE":
                if tool_calls:
                    tc = tool_calls[-1]
                    t_name = (tc.get("name") or "").lower()
                    t_args = tc.get("args") or {}

                    if t_name == "ask_question":
                        return ("needs_input", "Question for you!")
                    if t_name == "run_command":
                        bypass = self._clean_arg(t_args.get("BypassSandbox")).lower() == "true"
                        summary = self._clean_arg(
                            t_args.get("toolSummary") or t_args.get("CommandLine") or "Command"
                        )
                        if bypass:
                            return ("needs_input", f"Approval needed: {summary}")
                        return wrap_result("command", f"Terminal: {summary}")
                    if t_name in (
                        "read_file",
                        "view_file",
                        "view_file_outline",
                        "view_code_item",
                        "grep",
                        "grep_search",
                        "find_by_name",
                        "list_dir",
                    ):
                        p = self._clean_arg(t_args.get("Path") or t_args.get("Directory"))
                        fname = Path(p).name if p else "files"
                        return wrap_result("reading", f"Reading: {fname}")
                    if t_name in (
                        "write_file",
                        "edit_file",
                        "replace_file_content",
                        "multi_replace_file_content",
                    ):
                        p = self._clean_arg(t_args.get("Path") or t_args.get("TargetFile"))
                        fname = Path(p).name if p else "code"
                        return wrap_result("coding", f"Coding: {fname}")
                    if t_name in ("search_web", "read_url_content"):
                        q = self._clean_arg(t_args.get("query") or t_args.get("Url") or "Web")
                        return wrap_result("web", f"Web: {q}")
                    if t_name in ("invoke_subagent", "swarm", "run_workflow", "send_message"):
                        self.swarm_parent_path = self.last_path
                        r = self._clean_arg(t_args.get("Role") or t_args.get("name") or "Subagents")
                        return ("swarm", f"Swarm: {r}")
                    return wrap_result("thinking", f"Tool: {t_name}")
                else:
                    if swarm_active:
                        return ("swarm", "Swarm active...")
                    self.swarm_parent_path = None
                    content = last_obj.get("content") or ""
                    if "?" in content[-300:]:
                        return ("done", "Done! Waiting for reply")
                    return ("done", "Task completed!")

            if step_type == "GENERIC":
                return wrap_result("thinking", "Analyzing result...")
        except Exception:
            pass
        return None
