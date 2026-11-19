#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity Lifecycle Hook Handler for the Bee Companion Widget.
- Updates the bee's state in real time (zero tokens).
- Only auto-launches once on SessionStart if `auto_launch` is enabled.
"""

import json
import sys
from pathlib import Path
from platform_adapter import get_antigravity_host_pid
from state_sync import ensure_bee_running, read_state, write_state


def short_name(path_str: str) -> str:
    if not path_str:
        return ""
    try:
        return Path(path_str).name
    except Exception:
        return str(path_str)


def handle_hook(payload: dict) -> dict:
    event = payload.get("hookEventName", "")
    conv_id = payload.get("conversationId", "")
    host_pid = get_antigravity_host_pid()
    prev_state = read_state()
    swarm_active = prev_state.get("swarm_active", False)
    parent_conv_id = prev_state.get("parent_conv_id", "")
    is_subagent = bool(swarm_active and parent_conv_id and conv_id and conv_id != parent_conv_id)

    def set_bee(mode: str, detail: str, **kwargs):
        kwargs.setdefault("host_pid", host_pid)
        # While a subagent/swarm is active, keep the Queen Bee Swarm costume!
        if is_subagent and mode not in ("needs_input", "error"):
            write_state("swarm", f"Swarm: {detail}", **kwargs)
        else:
            write_state(mode, detail, **kwargs)

    if event == "SessionStart":
        if not is_subagent:
            write_state(
                "idle",
                "Session started",
                swarm_active=False,
                parent_conv_id=conv_id,
                host_pid=host_pid,
            )
        ensure_bee_running(force=False)
        return {}

    if event == "PreInvocation":
        if swarm_active:
            write_state("swarm", "Swarm thinking...", host_pid=host_pid)
        else:
            set_bee("thinking", "Thinking...")
        ensure_bee_running(force=False)
        return {}

    if event == "PreToolUse":
        tool_call = payload.get("toolCall", {}) or {}
        t_name = (tool_call.get("name") or "").lower()
        t_args = tool_call.get("args", {}) or {}

        if t_name == "ask_question":
            set_bee("needs_input", "Question for you!")
            return {"decision": "allow"}

        if t_name == "run_command":
            summary = t_args.get("toolSummary") or t_args.get("CommandLine") or "Command"
            if t_args.get("BypassSandbox"):
                set_bee("needs_input", f"Approval needed: {summary}")
                return {"decision": "ask"}
            set_bee("command", f"Terminal: {summary}")
            return {"decision": "allow"}

        if t_name in ("read_file", "view_file", "grep", "find_by_name", "list_dir"):
            fname = short_name(t_args.get("Path") or t_args.get("Directory") or "")
            label = f"Reading: {fname}" if fname else "Reading files..."
            set_bee("reading", label)
            return {"decision": "allow"}

        if t_name in ("write_file", "edit_file"):
            raw_path = t_args.get("Path") or ""
            fname = short_name(raw_path)
            label = f"Coding: {fname}" if fname else "Writing code..."
            workspaces = payload.get("workspacePaths") or []
            in_workspace = True
            if raw_path and workspaces:
                try:
                    rp = Path(raw_path).resolve()
                    in_workspace = any(
                        rp == Path(w).resolve() or Path(w).resolve() in rp.parents
                        for w in workspaces
                    )
                except Exception:
                    in_workspace = True
            if not in_workspace:
                set_bee("needs_input", f"File permission: {fname}")
                return {"decision": "ask"}
            set_bee("coding", label)
            return {"decision": "allow"}

        if t_name == "search_web":
            query = t_args.get("query") or "Web"
            set_bee("web", f"Web: {query}")
            return {"decision": "allow"}

        if t_name in ("invoke_subagent", "swarm", "run_workflow", "send_message"):
            role = t_args.get("Role") or t_args.get("name") or "Subagents"
            write_state(
                "swarm",
                f"Swarm: {role}",
                swarm_active=True,
                parent_conv_id=parent_conv_id or conv_id,
                host_pid=host_pid,
            )
            return {"decision": "allow"}

        set_bee("thinking", f"Tool: {t_name}")
        return {"decision": "allow"}

    if event == "PostToolUse":
        tool_call = payload.get("toolCall", {}) or {}
        t_name = (tool_call.get("name") or "").lower()
        if t_name in ("invoke_subagent", "swarm", "run_workflow") or swarm_active:
            write_state("swarm", "Swarm working...", host_pid=host_pid)
            return {}
        err = payload.get("error", "")
        if err:
            set_bee("thinking", "Analyzing error...")
        else:
            set_bee("thinking", "Analyzing result...")
        return {}

    if event == "Stop":
        reason = payload.get("terminationReason", "NO_TOOL_CALL")
        fully_idle = payload.get("fullyIdle", True)

        if is_subagent and not fully_idle:
            write_state("swarm", "Swarm active...", host_pid=host_pid)
            return {"decision": "stop"}

        if swarm_active and not fully_idle:
            write_state(
                "swarm",
                "Swarm running...",
                swarm_active=True,
                parent_conv_id=parent_conv_id or conv_id,
                host_pid=host_pid,
            )
        elif reason == "ERROR":
            write_state("error", "Error encountered", swarm_active=False, parent_conv_id="", host_pid=host_pid)
        elif reason == "USER_CANCELED":
            write_state("idle", "Canceled", swarm_active=False, parent_conv_id="", host_pid=host_pid)
        else:
            if is_subagent:
                write_state("swarm", "Subagent finished...", host_pid=host_pid)
            else:
                write_state("done", "Task completed!", swarm_active=False, parent_conv_id="", host_pid=host_pid)
        return {"decision": "stop"}

    return {}


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    response = handle_hook(payload)
    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
