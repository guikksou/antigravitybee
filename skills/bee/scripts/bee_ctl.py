#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI Controller for the Antigravity Bee Companion Widget.
Cross-platform (Windows & Linux).
Usage:
  python bee_ctl.py start [detail]
  python bee_ctl.py stop [--disable-auto]
  python bee_ctl.py auto [on|off]
  python bee_ctl.py mode <mode> [detail]
  python bee_ctl.py honey
  python bee_ctl.py status
"""

import json
import sys
import time
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


def start_bee(initial_mode: str = "idle", detail: str = "Ready"):
    write_state(initial_mode, detail)
    _, pid = ensure_bee_running(force=True)
    time.sleep(0.3)
    print(f"Bee companion active (PID {pid}) in mode '{initial_mode}'.")


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
    if PID_FILE.exists():
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
            val = args[1].lower() in ("on", "true", "1", "yes", "enable")
        else:
            val = not cfg.get("auto_launch", True)
        cfg["auto_launch"] = val
        save_config(cfg)
        if val:
            ensure_bee_running(force=True)
        print(f"Auto-launch set to: {'ON' if val else 'OFF'}")
    elif cmd == "honey":
        write_state("honey", "Eating honey")
        ensure_bee_running(force=True)
        print("Fed honey to the Bee!")
    elif cmd == "size":
        sz = args[1].lower() if len(args) > 1 else "medium"
        alias = {"petit": "small", "moyen": "medium", "grand": "large"}
        sz = alias.get(sz, sz)
        if sz not in ("small", "medium", "large"):
            sz = "medium"
        cfg = load_config()
        cfg["size"] = sz
        save_config(cfg)
        write_state(f"size:{sz}", "")
        ensure_bee_running(force=True)
        print(f"Bee widget size set to: {sz}")
    elif cmd == "mode":
        mode = args[1].lower() if len(args) > 1 else "idle"
        detail = " ".join(args[2:]) if len(args) > 2 else ""
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
