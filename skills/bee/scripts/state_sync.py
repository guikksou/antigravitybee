#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
State, Config & Live Transcript Synchronization for Antigravity Bee Companion.
Shared across bee_widget.py, bee_hook.py, and bee_ctl.py.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from platform_adapter import is_pid_alive, spawn_detached_widget

STATE_DIR = Path(tempfile.gettempdir()) / "agy_bee"
STATE_FILE = STATE_DIR / "bee_state.json"
PID_FILE = STATE_DIR / "bee_pid.txt"
CONFIG_FILE = STATE_DIR / "bee_config.json"
WIDGET_SCRIPT = Path(__file__).resolve().parent / "bee_widget.py"

DEFAULT_CONFIG = {
    "auto_launch": True,
    "sound": True,
    "ghost": False,
    "size": "medium",
}


def load_config() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def write_state(
    mode: str,
    detail: str = "",
    swarm_active: bool | None = None,
    parent_conv_id: str | None = None,
    host_pid: int | None = None,
):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        prev = read_state()
        data = {
            "mode": mode,
            "detail": detail,
            "timestamp": time.time(),
            "swarm_active": prev.get("swarm_active", False) if swarm_active is None else swarm_active,
            "parent_conv_id": prev.get("parent_conv_id", "") if parent_conv_id is None else parent_conv_id,
            "host_pid": prev.get("host_pid", 0) if host_pid is None else host_pid,
        }
        tmp_file = STATE_DIR / "bee_state.tmp"
        tmp_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_file, STATE_FILE)
    except Exception:
        pass


def read_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def is_bee_running() -> tuple[bool, int]:
    if not PID_FILE.exists():
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
    if not force and spawn_lock.exists():
        try:
            if (now - spawn_lock.stat().st_mtime) < 4.0:
                return True, 0
        except Exception:
            pass
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        spawn_lock.write_text(str(now), encoding="utf-8")
    except Exception:
        pass

    new_pid = spawn_detached_widget(WIDGET_SCRIPT)
    if new_pid > 0:
        try:
            PID_FILE.write_text(str(new_pid), encoding="utf-8")
        except Exception:
            pass
    return True, new_pid


class TranscriptWatcher:
    """Watches active Antigravity transcript.jsonl files across CLI, IDE, and App."""

    def __init__(self):
        self.last_path: Path | None = None
        self.last_mtime: float = 0.0
        self.last_scan: float = 0.0
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

    def poll(self, now: float) -> tuple[str, str] | None:
        if self.last_path is None or (now - self.last_scan > 3.0):
            self.last_scan = now
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
                self.last_step = -1

        if not self.last_path or not self.last_path.exists():
            return None

        try:
            st = self.last_path.stat()
            if st.st_mtime <= self.last_mtime:
                return None
            self.last_mtime = st.st_mtime

            with open(self.last_path, "rb") as f:
                f.seek(max(0, st.st_size - 8192))
                chunk = f.read().decode("utf-8", errors="replace")
            lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
            if not lines:
                return None
            last_obj = json.loads(lines[-1])
            step_idx = last_obj.get("step_index", -1)
            if step_idx == self.last_step:
                return None
            self.last_step = step_idx

            step_type = last_obj.get("type", "")
            status = last_obj.get("status", "")
            tool_calls = last_obj.get("tool_calls") or []

            st_data = read_state()
            swarm_active = st_data.get("swarm_active", False) or (
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
                        p = self._clean_arg(t_args.get("Path"))
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
