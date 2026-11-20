#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI Controller for the Antigravity Bee Companion Widget.
Cross-platform (Windows & Linux).
Usage:
  python bee_ctl.py start [detail]
  python bee_ctl.py stop [--disable-auto]
  python bee_ctl.py auto [on|off]
  python bee_ctl.py size <small|medium|large|xlarge>
  python bee_ctl.py mode <mode> [detail]
  python bee_ctl.py honey
  python bee_ctl.py status
"""

import json
import sys
import time
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from platform_adapter import terminate_pid
from state_sync import (
    PID_FILE,
    ensure_bee_running,
    is_bee_running,
    load_config,
    read_state,
    save_config,
    write_state,
)

VALID_MODES = {
    "idle",
    "sleep",
    "thinking",
    "reading",
    "coding",
    "command",
    "web",
    "swarm",
    "needs_input",
    "done",
    "error",
    "honey",
}

TRUE_VALUES = {"on", "true", "1", "yes", "enable"}
FALSE_VALUES = {"off", "false", "0", "no", "disable"}


def start_bee(initial_mode: str = "idle", detail: str = "Ready"):
    # Clear any stale host_pid from a closed Antigravity session so manual start stays alive.
    write_state(initial_mode, detail, swarm_active=False, host_pid=0)
    _, spawned_pid = ensure_bee_running(force=True)
    time.sleep(0.35)
    running, live_pid = is_bee_running()
    if running:
        print(f"Bee companion active (PID {live_pid or spawned_pid}) in mode '{initial_mode}'.")
    else:
        print(f"Bee companion failed to stay active after start (spawned PID {spawned_pid}).")


def stop_bee(disable_auto: bool = False):
    if disable_auto:
        cfg = load_config()
        cfg["auto_launch"] = False
        save_config(cfg)
    write_state("quit_disable" if disable_auto else "quit", "Closed")
    time.sleep(0.35)
    running, pid = is_bee_running()
    if running:
        terminate_pid(pid)
    if PID_FILE.exists() and not PID_FILE.is_symlink():
        try:
            PID_FILE.unlink()
        except Exception:
            pass
    print(f"Bee companion stopped (auto_launch={'OFF' if disable_auto else 'unchanged'}).")


def main():
    args = sys.argv[1:]
    cmd = args[0].lower() if args else "start"

    if cmd == "start":
        detail = " ".join(args[1:]) if len(args) > 1 else "Ready"
        start_bee("idle", detail)
    elif cmd == "stop":
        disable = "--disable-auto" in args or "off" in args
        stop_bee(disable_auto=disable)
    elif cmd == "auto":
        cfg = load_config()
        if len(args) > 1:
            raw_val = args[1].lower().strip()
            if raw_val in TRUE_VALUES:
                val = True
            elif raw_val in FALSE_VALUES:
                val = False
            else:
                print(f"Invalid value '{args[1]}' for auto. Expected on/off (or true/false/1/0/yes/no/enable/disable).")
                sys.exit(1)
        else:
            val = not cfg.get("auto_launch", True)
        cfg["auto_launch"] = val
        save_config(cfg)
        if val:
            if not is_bee_running()[0]:
                prev = read_state()
                prev_mode = prev.get("mode", "idle")
                if prev_mode in ("quit", "quit_disable", "") or prev_mode.startswith("size:"):
                    prev_mode = "idle"
                write_state(prev_mode, prev.get("detail", "Ready"), host_pid=0)
            ensure_bee_running(force=True)
        print(f"Auto-launch set to: {'ON' if val else 'OFF'}")
    elif cmd == "honey":
        if not is_bee_running()[0]:
            write_state("honey", "Eating honey", host_pid=0)
        else:
            write_state("honey", "Eating honey")
        ensure_bee_running(force=True)
        print("Fed honey to the Bee!")
    elif cmd == "size":
        sz = " ".join(args[1:]).lower().strip() if len(args) > 1 else "medium"
        alias = {
            "petit": "small",
            "moyen": "medium",
            "grand": "large",
            "tres grand": "xlarge",
            "très grand": "xlarge",
            "extra large": "xlarge",
            "extralarge": "xlarge",
            "xl": "xlarge",
        }
        sz = alias.get(sz, sz)
        if sz not in ("small", "medium", "large", "xlarge"):
            sz = "medium"
        cfg = load_config()
        cfg["size"] = sz
        save_config(cfg)
        prev = read_state()
        prev_mode = prev.get("mode", "idle")
        if not prev_mode or prev_mode in ("quit", "quit_disable") or prev_mode.startswith("size:"):
            prev_mode = "idle"
        prev_detail = prev.get("detail", "Ready") if prev_mode != "idle" else "Ready"
        if not is_bee_running()[0]:
            write_state(prev_mode, prev_detail, swarm_active=False, host_pid=0)
        else:
            write_state(prev_mode, prev_detail)
        ensure_bee_running(force=True)
        print(f"Bee widget size set to: {sz}")
    elif cmd == "mode":
        mode = args[1].lower() if len(args) > 1 else "idle"
        if mode not in VALID_MODES:
            print(f"Invalid mode '{mode}'. Valid modes: {', '.join(sorted(VALID_MODES))}")
            sys.exit(1)
        detail = " ".join(args[2:]) if len(args) > 2 else ""
        if not is_bee_running()[0]:
            write_state(mode, detail, host_pid=0)
        else:
            write_state(mode, detail)
        ensure_bee_running(force=True)
        print(f"Bee state updated to '{mode}': {detail}")
    elif cmd == "status":
        running, pid = is_bee_running()
        cfg = load_config()
        state = read_state()
        print(
            json.dumps(
                {
                    "running": running,
                    "pid": pid,
                    "auto_launch": cfg.get("auto_launch", True),
                    "size": cfg.get("size", "medium"),
                    "state": state,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
