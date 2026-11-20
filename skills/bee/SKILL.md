---
name: bee
description: >-
  Launch, control, or configure the always-on-top Pixel-Art Antigravity Bee companion widget (Windows & Linux).
  Activate this skill when the user invokes /bee or asks to start, stop, feed honey, resize, toggle auto-launch, or change the mode/costume of the bee widget.
---

# Antigravity Bee Companion (`/bee`)

This skill controls the **Antigravity Bee** pixel-art desktop overlay (Windows & Linux).
By default after installation, the widget launches automatically via lifecycle hooks (`hooks.json`) with zero token overhead. If auto-launch is disabled, the user can summon or control the widget on demand using `/bee`.

> **Backend selection**: Windows and Linux X11 use the standard `tkinter` GUI (`bee_widget.py`). Linux Wayland sessions (Hyprland, Sway, GNOME/KDE Wayland) automatically dispatch to the 32-bit ARGB GTK3/Cairo backend (`wayland_widget.py`) for true per-pixel alpha transparency, falling back to `tkinter` if PyGObject/GTK3 is unavailable (override with `AGY_BEE_BACKEND=tk` or `wayland`).

## Available Commands

Use the CLI controller [bee_ctl.py](./scripts/bee_ctl.py) via `run_command` (on Linux systems where only `python3` is installed, use `python3` in place of `python`):

1. **Start or wake the bee** (`/bee` or `/bee start`):
   ```bash
   python "<path_to_skill>/scripts/bee_ctl.py" start "Ready!"
   ```

2. **Enable or disable auto-launch on startup** (`/bee auto on` or `/bee auto off`):
   ```bash
   python "<path_to_skill>/scripts/bee_ctl.py" auto on
   python "<path_to_skill>/scripts/bee_ctl.py" auto off
   ```

3. **Feed honey to the bee** (`/bee honey`):
   ```bash
   python "<path_to_skill>/scripts/bee_ctl.py" honey
   ```

4. **Change widget size** (`/bee size <small|medium|large|xlarge>`):
   Supported sizes: `small` (`3x`), `medium` (`4x`), `large` (`5x`), `xlarge` (`7x`).
   ```bash
   python "<path_to_skill>/scripts/bee_ctl.py" size <small|medium|large|xlarge>
   ```

5. **Manually set costume / mode** (`/bee mode <mode>`):
   Supported modes: `idle`, `sleep`, `thinking`, `reading`, `coding`, `command`, `web`, `swarm`, `needs_input`, `done`, `error`.
   ```bash
   python "<path_to_skill>/scripts/bee_ctl.py" mode <mode> "<status_text>"
   ```

6. **Check status** (`/bee status`):
   ```bash
   python "<path_to_skill>/scripts/bee_ctl.py" status
   ```

7. **Stop / close the bee** (`/bee stop`):
   - Close temporarily: `python "<path_to_skill>/scripts/bee_ctl.py" stop`
   - Close and disable auto-launch: `python "<path_to_skill>/scripts/bee_ctl.py" stop --disable-auto`
