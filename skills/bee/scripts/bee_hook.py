#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity Lifecycle Hook Handler for the Bee Companion Widget.
- Updates the bee's state in real time (zero tokens).
- Only auto-launches once on SessionStart if `auto_launch` is enabled.
- Never overrides Antigravity's native tool permissions or stop/continue policy.
"""

import json
import sys
import time
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from platform_adapter import get_antigravity_host_pid
from state_sync import ensure_bee_running, read_state, write_state

READING_TOOLS = (
    "read_file",
    "view_file",
    "view_file_outline",
    "view_code_item",
    "grep",
    "grep_search",
    "find_by_name",
    "list_dir",
)
CODING_TOOLS = (
    "write_file",
    "edit_file",
    "replace_file_content",
    "multi_replace_file_content",
)
WEB_TOOLS = ("search_web", "read_url_content")
SWARM_TOOLS = ("invoke_subagent", "swarm", "run_workflow", "send_message")

SWARM_STALE_SECONDS = 120.0


def short_name(path_str: str) -> str:
    if not path_str:
        return ""
    try:
        return Path(path_str).name
    except Exception:
        return str(path_str)


def _is_path_allowed(raw_path: str, payload: dict) -> bool:
    """Check whether raw_path falls inside workspacePaths, artifactDirectoryPath, or ~/.gemini brain/scratch."""
    if not raw_path:
        return True
    workspaces = payload.get("workspacePaths") or []
    artifact_dir = payload.get("artifactDirectoryPath") or ""
    if not workspaces and not artifact_dir:
        return True

    allowed_roots = list(workspaces)
    if artifact_dir:
        allowed_roots.append(artifact_dir)

    gemini_dir = Path.home() / ".gemini"
    for prod in ("antigravity-cli", "antigravity", "antigravity-ide"):
        allowed_roots.append(str(gemini_dir / prod / "brain"))
        allowed_roots.append(str(gemini_dir / prod / "scratch"))
    allowed_roots.append(str(gemini_dir / "brain"))
    allowed_roots.append(str(gemini_dir / "scratch"))

    try:
        p = Path(raw_path).expanduser()
        if not p.is_absolute():
            base_cwd = Path(payload.get("cwd") or ".").expanduser()
            rp = (base_cwd / p).resolve()
        else:
            rp = p.resolve()

        for root in allowed_roots:
            if not root:
                continue
            rr = Path(root).expanduser().resolve()
            if rp == rr or rr in rp.parents:
                return True
        return False
    except Exception:
        return True


def handle_hook(payload: dict) -> dict:
    event = payload.get("hookEventName", "")
    conv_id = payload.get("conversationId", "")
    transcript_path = payload.get("transcriptPath", "") or ""
    tp_arg = transcript_path if transcript_path else None
    host_pid = get_antigravity_host_pid()

    prev_state = read_state()
    swarm_active = bool(prev_state.get("swarm_active", False))
    parent_conv_id = prev_state.get("parent_conv_id", "")
    prev_ts = float(prev_state.get("timestamp", 0.0) or 0.0)
    swarm_fresh = (time.time() - prev_ts) < SWARM_STALE_SECONDS

    is_subagent = bool(
        swarm_active
        and parent_conv_id
        and conv_id
        and conv_id != parent_conv_id
        and (event != "SessionStart" or swarm_fresh)
    )

    def set_bee(mode: str, detail: str, **kwargs):
        kwargs.setdefault("host_pid", host_pid)
        kwargs.setdefault("transcript_path", tp_arg)
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
                transcript_path=transcript_path,
            )
        elif tp_arg:
            write_state(
                "swarm",
                "Swarm active...",
                host_pid=host_pid,
                transcript_path=tp_arg,
            )
        ensure_bee_running(force=False)
        return {}

    if event == "PreInvocation":
        if swarm_active:
            write_state("swarm", "Swarm thinking...", host_pid=host_pid, transcript_path=tp_arg)
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

        if t_name in READING_TOOLS:
            fname = short_name(t_args.get("Path") or t_args.get("Directory") or "")
            label = f"Reading: {fname}" if fname else "Reading files..."
            set_bee("reading", label)
            return {"decision": "allow"}

        if t_name in CODING_TOOLS:
            raw_path = t_args.get("Path") or t_args.get("TargetFile") or ""
            fname = short_name(raw_path)
            label = f"Coding: {fname}" if fname else "Writing code..."
            if not _is_path_allowed(raw_path, payload):
                set_bee("needs_input", f"File permission: {fname}")
                return {"decision": "ask"}
            set_bee("coding", label)
            return {"decision": "allow"}

        if t_name in WEB_TOOLS:
            query = t_args.get("query") or t_args.get("Url") or "Web"
            set_bee("web", f"Web: {query}")
            return {"decision": "allow"}

        if t_name in SWARM_TOOLS:
            role = t_args.get("Role") or t_args.get("name") or "Subagents"
            write_state(
                "swarm",
                f"Swarm: {role}",
                swarm_active=True,
                parent_conv_id=parent_conv_id or conv_id,
                host_pid=host_pid,
                transcript_path=tp_arg,
            )
            return {"decision": "allow"}

        set_bee("thinking", f"Tool: {t_name}")
        return {"decision": "allow"}

    if event == "PostToolUse":
        tool_call = payload.get("toolCall", {}) or {}
        t_name = (tool_call.get("name") or "").lower()
        if t_name in SWARM_TOOLS or swarm_active:
            write_state("swarm", "Swarm working...", host_pid=host_pid, transcript_path=tp_arg)
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
            write_state("swarm", "Swarm active...", host_pid=host_pid, transcript_path=tp_arg)
            return {"decision": "stop"}

        if swarm_active and not fully_idle:
            write_state(
                "swarm",
                "Swarm running...",
                swarm_active=True,
                parent_conv_id=parent_conv_id or conv_id,
                host_pid=host_pid,
                transcript_path=tp_arg,
            )
        elif reason == "ERROR":
            write_state(
                "error",
                "Error encountered",
                swarm_active=False,
                parent_conv_id="",
                host_pid=host_pid,
                transcript_path=tp_arg,
            )
        elif reason == "USER_CANCELED":
            write_state(
                "idle",
                "Canceled",
                swarm_active=False,
                parent_conv_id="",
                host_pid=host_pid,
                transcript_path=tp_arg,
            )
        else:
            if is_subagent:
                write_state("swarm", "Subagent finished...", host_pid=host_pid, transcript_path=tp_arg)
            else:
                write_state(
                    "done",
                    "Task completed!",
                    swarm_active=False,
                    parent_conv_id="",
                    host_pid=host_pid,
                    transcript_path=tp_arg,
                )
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
