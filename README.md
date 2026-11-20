# Antigravity Bee Companion

![Antigravity Bee Preview](./preview.gif)

A lightweight, 60-FPS pixel-art desktop overlay for Google Antigravity (CLI, IDE, and Antigravity 2.0) on Windows and Linux.

It runs as an always-on-top desktop widget and reacts in real time (with zero token overhead) to the agent's execution loop: thinking, reading files, editing code, running terminal commands, orchestrating subagents, waiting for user input, or completing a task.

## Features

- **Zero-Token Lifecycle Synchronization**
  - Uses Antigravity lifecycle hooks (`hooks.json`) and a live `transcript.jsonl` tailer to update animations locally without querying the model.
  - Auto-launches on `SessionStart` by default. Can be disabled at any time from the right-click context menu or via `/bee auto off`, after which it can be started on demand with `/bee`.
- **State Transitions**
  - Working states: Thinking (glasses), Reading (magnifying glass), Coding (pencil), Terminal (hard-hat and prompt), Web (goggles), Swarm (crown and orbiting worker bees).
  - Notification states: Needs Input (`[!]`) alerts when user approval or an answer is required; Done (`[✓]`) displays a green completion badge, transitions automatically to `Idle` after 45 seconds, and enters `Sleep` after 90 seconds of inactivity.
- **Desktop Interactions**
  - 4 integer pixel-art scale presets: Small (`3x`), Medium (`4x`), Large (`5x`), and Extra Large (`7x`).
  - Subtle 1-pixel eye tracking towards the mouse cursor.
  - Mouse petting detection, click-to-acknowledge alerts, drag-and-drop with inertia, and Ghost Mode (45% opacity while idle/working, 100% when hovered or alerting).

## Installation

Clone this repository as `.agents` in your workspace root (or inside `~/.gemini/config/plugins/antigravitybee`):

```bash
git clone https://github.com/guikksou/antigravitybee.git .agents
```

The widget launches automatically on your next Antigravity session. You can also control it manually using the `/bee` slash command.

## Requirements

- **Windows**: Python 3.10+ (uses standard library `tkinter` and `ctypes`).
- **Linux**: Python 3.10+ with `python3-tk` (`sudo apt install python3-tk`).

## Architecture

```text
.agents/
├── plugin.json                 # Antigravity plugin manifest
├── hooks.json                  # Lifecycle hooks configuration
└── skills/
    └── bee/
        ├── SKILL.md            # /bee slash command specification
        └── scripts/
            ├── platform_adapter.py  # OS abstraction layer (Win32 / Linux X11 & POSIX)
            ├── pixel_sprites.py     # Palette, 2D pixel grids, and PhotoImage cache
            ├── state_sync.py        # Config, atomic state I/O, and transcript watcher
            ├── bee_widget.py        # Tkinter 60-FPS GUI and mouse event loop
            ├── bee_hook.py          # Hook handler and SessionStart auto-launcher
            └── bee_ctl.py           # CLI controller (start, stop, auto, size, honey, mode)
```
