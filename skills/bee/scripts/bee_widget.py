#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity Bee Companion (Main GUI Widget)
Modular, cross-platform (Windows & Linux) Pixel-Art Desktop Pet.
"""

import math
import os
import random
import sys
import time
try:
    import tkinter as tk
except ImportError:
    tk = None
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from platform_adapter import (
    IS_LINUX,
    TRANSPARENT_KEY,
    acquire_single_instance_lock,
    apply_window_shape_rects,
    configure_overlay_window,
    init_dpi_awareness,
    is_pid_alive,
    is_wayland_session,
    set_window_opacity,
)
from pixel_sprites import PixelArtRenderer
from state_sync import (
    CONFIG_FILE,
    PID_FILE,
    STATE_DIR,
    STATE_FILE,
    TranscriptWatcher,
    load_config,
    read_state,
    save_config,
    write_state,
)

init_dpi_awareness()

DONE_TO_IDLE_SECONDS = 45.0
IDLE_TO_SLEEP_SECONDS = 90.0
MAX_PARTICLES = 40
UI_FONT_FAMILY = "DejaVu Sans Mono" if IS_LINUX else "Consolas"

SIZE_PRESETS = {
    "small": {"scale": 3, "width": 200, "height": 180},
    "medium": {"scale": 4, "width": 240, "height": 225},
    "large": {"scale": 5, "width": 290, "height": 270},
    "xlarge": {"scale": 7, "width": 390, "height": 360},
}


class KawaiiBeeWidget:
    def __init__(self):
        if tk is None:
            raise RuntimeError(
                "tkinter is not installed (and Wayland GTK3 backend was not used)."
            )
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._claim_single_instance()

        self.root = tk.Tk()
        self.root.title("Antigravity Bee")
        configure_overlay_window(self.root, TRANSPARENT_KEY)

        self.config = load_config()
        self.size_name = self.config.get("size", "medium")
        if self.size_name not in SIZE_PRESETS:
            self.size_name = "medium"
        preset = SIZE_PRESETS[self.size_name]
        self.pixel_scale = preset["scale"]
        self.width = preset["width"]
        self.height = preset["height"]

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        default_x = screen_w - self.width - 30
        default_y = screen_h - self.height - 70
        self.win_x, self.win_y = self._clamp_position(
            self.config.get("x", default_x),
            self.config.get("y", default_y),
        )

        self.root.geometry(f"{self.width}x{self.height}+{self.win_x}+{self.win_y}")
        self.root.configure(bg=TRANSPARENT_KEY)

        self.canvas = tk.Canvas(
            self.root,
            width=self.width,
            height=self.height,
            bg=TRANSPARENT_KEY,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.renderer = PixelArtRenderer(scale=self.pixel_scale)
        self.transcript_watcher = TranscriptWatcher()

        # State & Timers
        self.mode = "idle"
        self.detail = "Ready"
        self.host_pid = 0
        self.last_state_mtime_ns = 0
        try:
            self.last_config_mtime_ns = CONFIG_FILE.stat().st_mtime_ns if CONFIG_FILE.exists() else 0
        except Exception:
            self.last_config_mtime_ns = 0
        self.last_mode_change = time.time()
        self.last_interaction = time.time()
        self.ghost_mode = self.config.get("ghost", False)
        self.auto_launch = self.config.get("auto_launch", True)

        # Lifecycle & Caching
        self._quitting = False
        self._poll_after_id = None
        self._anim_after_id = None
        self._leave_after_id = None
        self._last_shape_key = None
        self._last_render_key = None
        self._last_dyn_key = None
        self._last_bub_rects = None
        self._shadow_id = None
        self._bee_img_id = None
        self._last_shadow_coords = None
        self._last_bee_pos = None
        self._last_bee_sprite = None
        self._cached_look_dx = 0
        self._cached_look_dy = 0
        self._last_pointer_poll = 0.0
        self._bubble_measure_cache = {}
        self._drag_moved_this_frame = False

        # Mouse & physics state
        self.is_dragging = False
        self.drag_start_x = 0
        self.drag_start_y = 0
        self.drag_moved = False
        self.vx = 0.0
        self.vy = 0.0

        self.is_hovered = False
        self.pet_score = 0.0
        self.last_mouse_x = None
        self.last_pet_dir = 0
        self.petting_until = 0.0
        self.eating_until = 0.0
        self.poke_until = 0.0

        self.bubble_text = ""
        self.bubble_until = 0.0

        self.particles = []
        self.next_blink = time.time() + random.uniform(2.5, 5.0)
        self.blink_until = 0.0

        self.t0 = time.time()
        self._last_anim_time = self.t0
        self._last_ambient_particle_time = 0.0

        # Bindings
        self.canvas.bind("<ButtonPress-1>", self.on_left_down)
        self.canvas.bind("<B1-Motion>", self.on_left_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_left_up)
        self.canvas.bind("<Double-Button-1>", self.on_double_click)
        if IS_LINUX:
            self.canvas.bind("<ButtonRelease-3>", self.on_right_click)
        else:
            self.canvas.bind("<Button-3>", self.on_right_click)
        self.canvas.bind("<Enter>", self.on_mouse_enter)
        self.canvas.bind("<Leave>", self.on_mouse_leave)
        self.canvas.bind("<Motion>", self.on_mouse_move)

        self._build_context_menu()
        cur_st = read_state()
        self._last_state_data = cur_st
        try:
            if STATE_FILE.exists():
                self.last_state_mtime_ns = STATE_FILE.stat().st_mtime_ns
        except Exception:
            pass
        new_host_pid = int(cur_st.get("host_pid", 0) or 0) if cur_st else 0
        if new_host_pid > 0 and is_pid_alive(new_host_pid):
            self.host_pid = new_host_pid
        else:
            self.host_pid = 0
        init_mode = cur_st.get("mode", "") if cur_st else ""
        if not cur_st or init_mode in ("quit", "quit_disable", "", "honey") or init_mode.startswith("size:"):
            write_state("idle", "Idle", swarm_active=False, host_pid=self.host_pid)
            self._last_state_data = read_state()
            try:
                if STATE_FILE.exists():
                    self.last_state_mtime_ns = STATE_FILE.stat().st_mtime_ns
            except Exception:
                pass
        self._apply_opacity()

        self.poll_state()
        if not self._quitting:
            self.animate()

    def _get_virtual_screen_bounds(self) -> tuple[int, int, int, int]:
        """Return (min_x, min_y, max_x, max_y) across all connected monitors."""
        if not IS_LINUX and sys.platform == "win32":
            try:
                import ctypes
                u32 = ctypes.windll.user32
                vx = int(u32.GetSystemMetrics(76))
                vy = int(u32.GetSystemMetrics(77))
                vw = int(u32.GetSystemMetrics(78))
                vh = int(u32.GetSystemMetrics(79))
                if vw > 0 and vh > 0:
                    return vx, vy, vx + vw, vy + vh
            except Exception:
                pass
        try:
            vx = int(self.root.winfo_vrootx())
            vy = int(self.root.winfo_vrooty())
            vw = int(self.root.winfo_vrootwidth())
            vh = int(self.root.winfo_vrootheight())
            if vw > 0 and vh > 0:
                return vx, vy, vx + vw, vy + vh
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _clamp_position(self, x: int, y: int) -> tuple[int, int]:
        vx0, vy0, vx1, vy1 = self._get_virtual_screen_bounds()
        cx = max(vx0, min(vx1 - self.width, int(x)))
        cy = max(vy0, min(vy1 - self.height, int(y)))
        return cx, cy

    def _claim_single_instance(self):
        # 1. OS Kernel Mutex / flock check: if another bee is already running, exit immediately!
        lock_file = STATE_DIR / "bee.lock"
        if not acquire_single_instance_lock(lock_file):
            sys.exit(0)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    def _persist_config(self):
        latest = load_config()
        latest["x"] = self.win_x
        latest["y"] = self.win_y
        latest["ghost"] = self.ghost_mode
        latest["auto_launch"] = self.auto_launch
        latest["size"] = self.size_name
        latest.pop("sound", None)
        self.config = latest
        save_config(latest)
        try:
            if CONFIG_FILE.exists():
                self.last_config_mtime_ns = CONFIG_FILE.stat().st_mtime_ns
        except Exception:
            pass

    def set_size(self, size_name: str):
        size_name = size_name.lower()
        if size_name not in SIZE_PRESETS:
            return
        self.size_name = size_name
        preset = SIZE_PRESETS[size_name]
        self.pixel_scale = preset["scale"]
        self.width = preset["width"]
        self.height = preset["height"]
        self.renderer.set_scale(self.pixel_scale)
        self.canvas.configure(width=self.width, height=self.height)
        self.win_x, self.win_y = self._clamp_position(self.win_x, self.win_y)
        self.canvas.delete("all")
        self._shadow_id = None
        self._bee_img_id = None
        self._last_shadow_coords = None
        self._last_bee_pos = None
        self._last_bee_sprite = None
        self._last_render_key = None
        self._last_dyn_key = None
        self._last_bub_rects = None
        self._last_shape_key = None
        self.root.geometry(f"{self.width}x{self.height}+{self.win_x}+{self.win_y}")
        self._persist_config()

    def _build_context_menu(self):
        self.menu = tk.Menu(
            self.root,
            tearoff=0,
            bg="#2C1810",
            fg="#FFF394",
            activebackground="#FFCC33",
            activeforeground="#2C1810",
            font=(UI_FONT_FAMILY, 9, "bold"),
        )
        self.menu.add_command(label="Feed Honey", command=self.feed_honey)
        self.menu.add_command(label="Poke / Wave", command=self.trigger_poke)

        costume_menu = tk.Menu(
            self.menu,
            tearoff=0,
            bg="#2C1810",
            fg="#FFF394",
            activebackground="#FFCC33",
            activeforeground="#2C1810",
            font=(UI_FONT_FAMILY, 9),
        )
        modes = [
            ("Idle", "idle", "Idle"),
            ("Sleep (Cloud)", "sleep", "Zzz... Sleeping"),
            ("Thinking (Glasses)", "thinking", "Thinking..."),
            ("Reading (Magnifier)", "reading", "Reading files..."),
            ("Coding (Pencil)", "coding", "Writing code..."),
            ("Terminal (Hard-Hat)", "command", "Running command..."),
            ("Web (Goggles)", "web", "Searching web..."),
            ("Swarm Queen (Crown)", "swarm", "Subagents active"),
            ("Needs Input (!)", "needs_input", "Waiting for input!"),
            ("Done (Checkmark)", "done", "Task completed!"),
            ("Error", "error", "Error encountered"),
        ]
        for label, m_code, m_det in modes:
            costume_menu.add_command(
                label=label,
                command=lambda c=m_code, d=m_det: self.set_mode(c, d, manual=True),
            )
        self.menu.add_cascade(label="Preview Mode", menu=costume_menu)

        size_menu = tk.Menu(
            self.menu,
            tearoff=0,
            bg="#2C1810",
            fg="#FFF394",
            activebackground="#FFCC33",
            activeforeground="#2C1810",
            font=(UI_FONT_FAMILY, 9),
        )
        size_menu.add_command(label="Small (3x)", command=lambda: self.set_size("small"))
        size_menu.add_command(label="Medium (4x)", command=lambda: self.set_size("medium"))
        size_menu.add_command(label="Large (5x)", command=lambda: self.set_size("large"))
        size_menu.add_command(label="Extra Large (7x)", command=lambda: self.set_size("xlarge"))
        self.menu.add_cascade(label="Widget Size", menu=size_menu)

        self.menu.add_separator()
        self.menu.add_command(label="Ghost Mode (On/Off)", command=self.toggle_ghost_mode)
        self.menu.add_command(label="Auto-Launch (On/Off)", command=self.toggle_auto_launch)
        self.menu.add_command(label="Reset Position", command=self.reset_position)
        self.menu.add_separator()
        self.menu.add_command(
            label="Close & Disable Auto-Launch",
            command=lambda: self.quit_bee(disable_auto=True),
        )
        self.menu.add_command(
            label="Close",
            command=lambda: self.quit_bee(disable_auto=False),
        )

    # -------------------- Mouse Interactions --------------------

    def on_left_down(self, event):
        self.is_dragging = True
        self.drag_moved = False
        self.drag_start_x = event.x_root - self.win_x
        self.drag_start_y = event.y_root - self.win_y
        self.last_interaction = time.time()

    def on_left_drag(self, event):
        if not self.is_dragging:
            return
        new_x, new_y = self._clamp_position(
            event.x_root - self.drag_start_x,
            event.y_root - self.drag_start_y,
        )
        dx = new_x - self.win_x
        dy = new_y - self.win_y
        if abs(dx) > 3 or abs(dy) > 3:
            self.drag_moved = True
        self.vx = dx
        self.vy = dy
        self._drag_moved_this_frame = True
        self.win_x = new_x
        self.win_y = new_y
        self.root.geometry(f"+{self.win_x}+{self.win_y}")

    def on_left_up(self, event):
        self.is_dragging = False
        self.vx = 0.0
        self.vy = 0.0
        self._persist_config()

        if not self.drag_moved:
            if self.mode in ("needs_input", "done", "error"):
                self.set_mode("idle", "Idle", manual=True)
                write_state("idle", "Idle")
                self._last_state_data = read_state()
                try:
                    if STATE_FILE.exists():
                        self.last_state_mtime_ns = STATE_FILE.stat().st_mtime_ns
                except Exception:
                    pass
                self.bubble_until = 0.0
                self.spawn_particles("sparkle", 5)
            else:
                self.trigger_poke()

    def on_double_click(self, event):
        self.feed_honey()

    def on_right_click(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def on_mouse_enter(self, event):
        if self._leave_after_id is not None:
            try:
                self.root.after_cancel(self._leave_after_id)
            except Exception:
                pass
            self._leave_after_id = None
        self.is_hovered = True
        self._apply_opacity()

    def on_mouse_leave(self, event):
        if self._leave_after_id is not None:
            try:
                self.root.after_cancel(self._leave_after_id)
            except Exception:
                pass
        self._leave_after_id = self.root.after(120, self._confirm_mouse_leave)

    def _confirm_mouse_leave(self):
        self._leave_after_id = None
        if self._quitting:
            return
        try:
            mx, my = self.root.winfo_pointerxy()
            cx = self.win_x + self.width // 2
            base_cy = self.win_y + int(self.height * 0.54)
            rx = 16 * self.pixel_scale
            ry = 18 * self.pixel_scale
            if abs(mx - cx) <= rx and abs(my - base_cy) <= ry:
                return
        except Exception:
            pass
        self.is_hovered = False
        self.last_mouse_x = None
        self.pet_score = 0.0
        self._apply_opacity()

    def on_mouse_move(self, event):
        cx, cy = self.width // 2, int(self.height * 0.54)
        rx, ry = 15 * self.pixel_scale, 14 * self.pixel_scale
        if (cx - rx) <= event.x <= (cx + rx) and (cy - ry) <= event.y <= (cy + ry) and not self.is_dragging:
            if self.last_mouse_x is not None:
                dx = event.x - self.last_mouse_x
                if abs(dx) > 4:
                    direction = 1 if dx > 0 else -1
                    if direction != self.last_pet_dir:
                        self.pet_score += 1.0
                        self.last_pet_dir = direction
                        if self.pet_score >= 3.0:
                            self.trigger_petting()
                            self.pet_score = 0.0
            self.last_mouse_x = event.x

    def trigger_petting(self):
        now = time.time()
        self.last_interaction = now
        if self.mode == "sleep":
            self.mode = "idle"
        self.petting_until = now + 2.2
        self.spawn_particles("heart", 5)

    def trigger_poke(self):
        now = time.time()
        self.last_interaction = now
        if self.mode == "sleep":
            self.mode = "idle"
        self.poke_until = now + 0.6
        self.spawn_particles("sparkle", 5)

    def feed_honey(self):
        now = time.time()
        self.last_interaction = now
        if self.mode == "sleep":
            self.mode = "idle"
        self.eating_until = now + 3.2
        self.spawn_particles("heart", 6)

    def toggle_ghost_mode(self):
        self.ghost_mode = not self.ghost_mode
        self._persist_config()
        self._apply_opacity()
        self.show_bubble("Ghost Mode: " + ("ON" if self.ghost_mode else "OFF"), 2.0)

    def toggle_auto_launch(self):
        self.auto_launch = not self.auto_launch
        self._persist_config()
        status = "ON" if self.auto_launch else "OFF (/bee)"
        self.show_bubble(f"Auto-Launch: {status}", 3.0)

    def reset_position(self):
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.win_x, self.win_y = self._clamp_position(
            screen_w - self.width - 30,
            screen_h - self.height - 70,
        )
        self.root.geometry(f"+{self.win_x}+{self.win_y}")
        self._persist_config()

    def quit_bee(self, disable_auto: bool = False):
        if self._quitting:
            return
        self._quitting = True
        if disable_auto:
            self.auto_launch = False
            self._persist_config()
        for attr in ("_poll_after_id", "_anim_after_id", "_leave_after_id"):
            after_id = getattr(self, attr, None)
            if after_id is not None:
                try:
                    self.root.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)
        try:
            if PID_FILE.exists():
                PID_FILE.unlink()
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _apply_opacity(self):
        if self.is_hovered or self.mode in ("needs_input", "done", "error"):
            set_window_opacity(self.root, 1.0)
        elif self.ghost_mode:
            set_window_opacity(self.root, 0.45)
        else:
            set_window_opacity(self.root, 0.98)

    def show_bubble(self, text, duration=3.0):
        self.bubble_text = text
        self.bubble_until = time.time() + duration

    def spawn_particles(self, kind, count=5):
        cx, cy = self.width // 2, int(self.height * 0.52)
        s = self.pixel_scale / 4.0
        for _ in range(count):
            if len(self.particles) >= MAX_PARTICLES:
                self.particles.pop(0)
            if kind == "heart":
                self.particles.append({
                    "x": cx + random.uniform(-28 * s, 28 * s),
                    "y": cy - 20 * s + random.uniform(-10 * s, 10 * s),
                    "vx": random.uniform(-0.6, 0.6),
                    "vy": random.uniform(-1.8, -0.9),
                    "kind": "heart",
                    "life": 0,
                    "max_life": random.randint(26, 42),
                })
            elif kind == "sparkle":
                angle = random.uniform(0, 2 * math.pi)
                spd = random.uniform(1.0, 2.4)
                self.particles.append({
                    "x": cx + random.uniform(-16 * s, 16 * s),
                    "y": cy + random.uniform(-16 * s, 16 * s),
                    "vx": math.cos(angle) * spd,
                    "vy": math.sin(angle) * spd - 0.4,
                    "kind": "sparkle",
                    "life": 0,
                    "max_life": random.randint(18, 32),
                })
            elif kind == "zzz":
                self.particles.append({
                    "x": cx + 22 * s + random.uniform(-4, 4),
                    "y": cy - 22 * s,
                    "vx": random.uniform(0.3, 0.6),
                    "vy": random.uniform(-1.0, -0.6),
                    "kind": "zzz",
                    "life": 0,
                    "max_life": 46,
                })

    # -------------------- State & Lifecycle Transitions --------------------

    def set_mode(self, new_mode, new_detail, manual=False):
        old_mode = self.mode
        now = time.time()
        self.mode = new_mode
        self.detail = new_detail or ""
        self.last_mode_change = now
        self.last_interaction = now
        self._apply_opacity()

        if new_mode == "honey":
            self.mode = "idle"
            self.feed_honey()
            return

        if new_mode != old_mode or manual:
            if new_mode == "needs_input":
                self.show_bubble(f"! {self.detail or 'Waiting for user input!'}", 12.0)
                self.spawn_particles("sparkle", 6)
            elif new_mode == "done":
                self.show_bubble(f"OK: {self.detail or 'Task completed!'}", 6.0)
            elif new_mode == "error":
                self.show_bubble(f"ERR: {self.detail or 'Error encountered'}", 8.0)
            elif self.detail:
                self.show_bubble(self.detail, 3.5)

    def poll_state(self):
        if self._quitting:
            return
        now = time.time()
        try:
            if CONFIG_FILE.exists():
                cfg_mtime_ns = CONFIG_FILE.stat().st_mtime_ns
                if cfg_mtime_ns != self.last_config_mtime_ns:
                    self.last_config_mtime_ns = cfg_mtime_ns
                    new_cfg = load_config()
                    self.config = new_cfg
                    self.auto_launch = bool(new_cfg.get("auto_launch", True))
                    new_ghost = bool(new_cfg.get("ghost", False))
                    if new_ghost != self.ghost_mode:
                        self.ghost_mode = new_ghost
                        self._apply_opacity()
                    new_size = str(new_cfg.get("size", "medium")).lower()
                    if new_size in SIZE_PRESETS and new_size != self.size_name:
                        self.set_size(new_size)
        except Exception:
            pass

        try:
            if STATE_FILE.exists():
                mtime_ns = STATE_FILE.stat().st_mtime_ns
                if mtime_ns != self.last_state_mtime_ns:
                    data = read_state()
                    if data:
                        self.last_state_mtime_ns = mtime_ns
                        self._last_state_data = data
                        new_host_pid = int(data.get("host_pid", 0) or 0)
                        if new_host_pid > 0 and is_pid_alive(new_host_pid):
                            self.host_pid = new_host_pid
                        else:
                            self.host_pid = 0
                        new_mode = data.get("mode", "idle")
                        new_detail = data.get("detail", "")
                        if new_mode == "quit":
                            self.quit_bee(disable_auto=False)
                            return
                        if new_mode == "quit_disable":
                            self.quit_bee(disable_auto=True)
                            return
                        if new_mode.startswith("size:"):
                            self.set_size(new_mode.split(":", 1)[1])
                            write_state(self.mode, self.detail or "Idle")
                            self._last_state_data = read_state()
                            if STATE_FILE.exists():
                                self.last_state_mtime_ns = STATE_FILE.stat().st_mtime_ns
                        elif new_mode == "honey":
                            self.set_mode(new_mode, new_detail)
                            write_state(self.mode, self.detail or "Idle")
                            self._last_state_data = read_state()
                            if STATE_FILE.exists():
                                self.last_state_mtime_ns = STATE_FILE.stat().st_mtime_ns
                        else:
                            self.set_mode(new_mode, new_detail)
        except Exception:
            pass

        # Auto-close if the Antigravity host process was closed!
        if self.host_pid > 0 and not is_pid_alive(self.host_pid):
            self.quit_bee(disable_auto=False)
            return

        # Live transcript polling
        t_res = self.transcript_watcher.poll(now, state_data=self._last_state_data)
        if t_res is not None:
            self.set_mode(t_res[0], t_res[1])

        # Automatic calm phase transitions:
        # 1) Done -> Idle after 45 seconds
        if self.mode == "done" and (now - self.last_mode_change >= DONE_TO_IDLE_SECONDS):
            self.mode = "idle"
            self.detail = "Idle"
            self.last_mode_change = now
            self.last_interaction = now
            self._apply_opacity()

        # 2) Idle -> Sleep after 90 seconds of inactivity
        elif self.mode == "idle" and (now - self.last_interaction >= IDLE_TO_SLEEP_SECONDS):
            self.mode = "sleep"
            self.detail = "Zzz... Sleeping"
            self.last_mode_change = now

        if not self._quitting:
            self._poll_after_id = self.root.after(250, self.poll_state)

    # -------------------- Adaptive Pixel-Art Animation Loop --------------------

    def animate(self):
        if self._quitting:
            return
        now = time.time()
        t = now - self.t0
        dt = min(max(0.0, now - self._last_anim_time), 0.25)
        self._last_anim_time = now
        dt_60 = dt * 60.0

        eff_mode = self.mode
        if self.is_dragging:
            eff_mode = "dragged"
        elif now < self.eating_until:
            eff_mode = "eating"
        elif now < self.petting_until:
            eff_mode = "petting"

        sc = self.pixel_scale
        cx = self.width // 2
        base_cy = int(self.height * 0.54)

        if eff_mode == "dragged":
            bob_y = 0
            bob_x = 0
        elif eff_mode == "needs_input":
            bob_y = int(-abs(math.sin(t * 9.0)) * 4) * sc
            bob_x = int(math.sin(t * 14.0)) * sc
        elif eff_mode == "sleep":
            bob_y = int(math.sin(t * 1.8) * 1) * sc + 2 * sc
            bob_x = 0
        else:
            # Calm gentle float for idle, done, thinking, reading, etc.
            bob_y = int(math.sin(t * 3.5) * 2) * sc
            bob_x = 0

        if now < self.poke_until:
            prog = (self.poke_until - now) / 0.6
            bob_y -= int(math.sin(prog * math.pi) * 4) * sc

        bx = cx + bob_x
        by = base_cy + bob_y

        # Ambient pixel particles (time-based intervals matching 60fps cadence)
        if eff_mode == "sleep":
            if now - self._last_ambient_particle_time >= 40.0 / 60.0:
                self._last_ambient_particle_time = now
                self.spawn_particles("zzz", 1)
        elif eff_mode == "coding":
            if now - self._last_ambient_particle_time >= 20.0 / 60.0:
                self._last_ambient_particle_time = now
                self.spawn_particles("sparkle", 1)
        elif eff_mode in ("petting", "eating"):
            if now - self._last_ambient_particle_time >= 16.0 / 60.0:
                self._last_ambient_particle_time = now
                self.spawn_particles("heart", 1)

        # Update particle physics before computing render_key
        if self.particles:
            self._update_particles(dt_60)

        # Wing speed: fast only when actively working or alerting; calm in idle & done!
        if eff_mode in ("needs_input", "coding", "command", "swarm", "dragged"):
            wing_frame = int(t * 30.0) % 3
        else:
            wing_frame = int(t * 12.0) % 3

        # Eye expression
        if now >= self.next_blink:
            self.blink_until = now + 0.14
            self.next_blink = now + random.uniform(2.8, 5.5)
        is_blinking = now < self.blink_until

        if eff_mode == "sleep" or is_blinking:
            eye_state = "sleep"
        elif eff_mode in ("petting", "eating"):
            eye_state = "happy"
        elif eff_mode == "error":
            eye_state = "dizzy"
        elif eff_mode in ("dragged", "needs_input"):
            eye_state = "surprised"
        else:
            # Calm normal eyes in idle AND done (with normal blinking)
            eye_state = "normal"

        # Subtle 1-pixel eye tracking towards mouse cursor (throttled to ~0.20s, skipped in sleep/reading)
        if eff_mode == "reading":
            look_dx = -1 if int(t * 3) % 2 == 0 else 1
            look_dy = 0
        elif eff_mode == "sleep":
            look_dx = self._cached_look_dx
            look_dy = self._cached_look_dy
        else:
            if now - self._last_pointer_poll >= 0.20:
                self._last_pointer_poll = now
                try:
                    mx, my = self.root.winfo_pointerxy()
                    rel_x = mx - (self.win_x + bx)
                    rel_y = my - (self.win_y + by)
                    ldx = -1 if rel_x < -45 else (1 if rel_x > 45 else 0)
                    ldy = 1 if rel_y > 45 else 0
                    self._cached_look_dx = ldx
                    self._cached_look_dy = ldy
                except Exception:
                    pass
            look_dx = self._cached_look_dx
            look_dy = self._cached_look_dy

        if not self._drag_moved_this_frame:
            self.vx = 0.0
            self.vy = 0.0
        self._drag_moved_this_frame = False

        prop_frame = int(t * 4) % 12
        leg_shift = 1 if self.vx < -3 else (-1 if self.vx > 3 else 0)

        shadow_w = (6 * sc) if bob_y >= 0 else (4 * sc)
        shadow_y = base_cy + 15 * sc

        # Precompute orbiting mini-bee positions once per frame in swarm mode
        mini_bee_positions = []
        mb_frame = int(t * 20.0) % 2 if eff_mode == "swarm" else -1
        if eff_mode == "swarm":
            for i in range(3):
                angle = t * 2.6 + i * (2 * math.pi / 3)
                z = math.sin(angle)
                mx = int((bx + math.cos(angle) * (15.5 * sc)) // sc) * sc
                my = int((by + z * (5.0 * sc)) // sc) * sc
                mini_bee_positions.append((mx, my, z))

        norm_key = self.renderer._normalize_key(
            eff_mode, wing_frame, eye_state, look_dx, look_dy, prop_frame, leg_shift
        )

        particle_state = tuple(
            (int(p["x"] // 2) * 2, int(p["y"] // 2) * 2, p["kind"])
            for p in self.particles
        )

        if self.is_hovered:
            txt = self.bubble_text if now < self.bubble_until and self.bubble_text else (self.detail or "Ready")
            offset_top = (16 * sc) if eff_mode in ("needs_input", "error") else (12 * sc)
            bub_y = by - offset_top
            bubble_state = (bx, bub_y, txt, eff_mode)
        else:
            bubble_state = None

        mini_bee_state = (
            (mb_frame, tuple((mx, my, z >= 0) for mx, my, z in mini_bee_positions))
            if eff_mode == "swarm"
            else None
        )

        render_key = (
            self.pixel_scale,
            bx,
            by,
            norm_key,
            shadow_w,
            bubble_state,
            mini_bee_state,
            particle_state,
        )

        if render_key != self._last_render_key:
            # 1. Ground shadow (reused persistent canvas item)
            shadow_coords = (cx - shadow_w, shadow_y, cx + shadow_w, shadow_y + sc)
            if self._shadow_id is None:
                self._shadow_id = self.canvas.create_rectangle(
                    *shadow_coords, fill="#2C1810", outline=""
                )
            elif self._last_shadow_coords != shadow_coords:
                self.canvas.coords(self._shadow_id, *shadow_coords)
            self._last_shadow_coords = shadow_coords

            # 2. Main Pixel-Art Bee Sprite (reused persistent canvas item)
            sprite = self.renderer.get_bee_sprite(
                eff_mode, wing_frame, eye_state, look_dx, look_dy, prop_frame, leg_shift
            )
            if self._bee_img_id is None:
                self._bee_img_id = self.canvas.create_image(bx, by, image=sprite, anchor="center")
            else:
                if self._last_bee_pos != (bx, by):
                    self.canvas.coords(self._bee_img_id, bx, by)
                if self._last_bee_sprite is not sprite:
                    self.canvas.itemconfigure(self._bee_img_id, image=sprite)
            self._last_bee_pos = (bx, by)
            self._last_bee_sprite = sprite

            # 3. Dynamic tagged items (mini-bees, particles, speech bubble)
            dyn_key = (bubble_state, mini_bee_state, particle_state)
            if dyn_key != self._last_dyn_key:
                self.canvas.delete("dyn")
                if eff_mode == "swarm":
                    self._draw_mini_bees(mini_bee_positions, mb_frame, behind=True)
                    self._draw_mini_bees(mini_bee_positions, mb_frame, behind=False)
                self._draw_particles()
                bub_rects = None
                if bubble_state is not None:
                    bub_rects = self._draw_pixel_speech_box(*bubble_state)
                self._last_bub_rects = bub_rects
                self._last_dyn_key = dyn_key
            else:
                bub_rects = self._last_bub_rects

            # 4. Linux X11 XShape pixel-perfect transparency mask (no-op on Windows)
            if IS_LINUX:
                shape_key = (
                    bx,
                    by,
                    norm_key,
                    shadow_w,
                    tuple((int(p["x"] // 2), int(p["y"] // 2), p["kind"]) for p in self.particles),
                    tuple(bub_rects) if bub_rects else None,
                    mb_frame,
                    tuple((mx, my) for mx, my, _ in mini_bee_positions) if eff_mode == "swarm" else None,
                )
                if shape_key != self._last_shape_key:
                    shape_rects = [
                        (cx - shadow_w, shadow_y, 2 * shadow_w, sc)
                    ]
                    # Main bee sprite top-left offset (44x40 base grid anchored at center)
                    bee_x0 = bx - 22 * sc
                    bee_y0 = by - 20 * sc
                    for rx, ry, rw, rh in self.renderer.get_bee_rects(
                        eff_mode, wing_frame, eye_state, look_dx, look_dy, prop_frame, leg_shift
                    ):
                        shape_rects.append((bee_x0 + rx, bee_y0 + ry, rw, rh))

                    # Mini bees in swarm mode
                    if eff_mode == "swarm":
                        mb_key = f"mini_bee_{mb_frame}"
                        mb_rects, mb_w, mb_h = self.renderer.particle_rects_cache.get(mb_key, ([], 0, 0))
                        for mx, my, _ in mini_bee_positions:
                            mx0 = mx - mb_w // 2
                            my0 = my - mb_h // 2
                            for rx, ry, rw, rh in mb_rects:
                                shape_rects.append((mx0 + rx, my0 + ry, rw, rh))

                    # Active particles
                    for p in self.particles:
                        p_rects, pw, ph = self.renderer.particle_rects_cache.get(p["kind"], ([], 0, 0))
                        px = int(p["x"] // 2) * 2 - pw // 2
                        py = int(p["y"] // 2) * 2 - ph // 2
                        for rx, ry, rw, rh in p_rects:
                            shape_rects.append((px + rx, py + ry, rw, rh))

                    # Speech bubble
                    if bub_rects:
                        shape_rects.extend(bub_rects)

                    if apply_window_shape_rects(self.root, shape_rects):
                        self._last_shape_key = shape_key

            self._last_render_key = render_key

        if not self._quitting:
            if (
                eff_mode in ("needs_input", "dragged", "swarm", "eating", "petting")
                or now < self.poke_until
                or len(self.particles) > 0
            ):
                next_ms = 42
            elif eff_mode == "sleep":
                next_ms = 166
            else:
                next_ms = 83
            self._anim_after_id = self.root.after(next_ms, self.animate)

    def _draw_mini_bees(self, mini_bee_positions, mb_frame: int, behind=True):
        mb_sprite = self.renderer.particle_cache[f"mini_bee_{mb_frame}"]
        for mx, my, z in mini_bee_positions:
            if (behind and z >= 0) or (not behind and z < 0):
                continue
            item_id = self.canvas.create_image(mx, my, image=mb_sprite, anchor="center", tags=("dyn",))
            if behind and self._bee_img_id is not None:
                self.canvas.tag_lower(item_id, self._bee_img_id)

    def _update_particles(self, dt_60: float):
        alive = []
        for p in self.particles:
            p["life"] += dt_60
            if p["life"] < p["max_life"]:
                p["x"] += p["vx"] * dt_60
                p["y"] += p["vy"] * dt_60
                alive.append(p)
        self.particles = alive

    def _draw_particles(self):
        for p in self.particles:
            spr = self.renderer.particle_cache.get(p["kind"])
            if spr:
                px = int(p["x"] // 2) * 2
                py = int(p["y"] // 2) * 2
                self.canvas.create_image(px, py, image=spr, anchor="center", tags=("dyn",))

    def _draw_pixel_speech_box(self, x, y, text, eff_mode):
        if len(text) > 28:
            text = text[:26] + ".."

        bg_col = "#FFF9E6"
        border_col = "#2C1810"
        inner_border = "#FFCC33"
        text_col = "#2C1810"

        if eff_mode == "needs_input":
            bg_col = "#FEF2F2"
            inner_border = "#EF4444"
            text_col = "#991B1B"
        elif eff_mode == "done":
            bg_col = "#F0FDF4"
            inner_border = "#22C55E"
            text_col = "#166534"
        elif eff_mode == "error":
            bg_col = "#FFFBEB"
            inner_border = "#F59E0B"
            text_col = "#92400E"

        # Create the text item first to measure its true rendered pixel bounding box
        # across different DPI settings (e.g. 96 DPI on Windows vs 192 DPI on Linux).
        font_px = -max(12, int(self.pixel_scale * 3.0))
        cache_key = (text, self.width, font_px)
        cached = self._bubble_measure_cache.get(cache_key)
        if cached is not None:
            disp_text, w, h = cached
            text_id = self.canvas.create_text(
                x, y,
                text=disp_text,
                fill=text_col,
                font=(UI_FONT_FAMILY, font_px, "bold"),
                tags=("dyn",),
            )
        else:
            text_id = self.canvas.create_text(
                x, y,
                text=text,
                fill=text_col,
                font=(UI_FONT_FAMILY, font_px, "bold"),
                tags=("dyn",),
            )
            bbox = self.canvas.bbox(text_id)
            if bbox:
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
            else:
                tw = len(text) * abs(font_px)
                th = abs(font_px) + 4

            pad_w = 32
            pad_h = 20
            max_w = self.width - 8
            disp_text = text
            # If the rendered text is still wider than the widget canvas, trim characters
            while tw + pad_w > max_w and len(disp_text) > 5:
                if disp_text.endswith(".."):
                    disp_text = disp_text[:-3] + ".."
                else:
                    disp_text = disp_text[:-2] + ".."
                self.canvas.itemconfigure(text_id, text=disp_text)
                bbox = self.canvas.bbox(text_id)
                if bbox:
                    tw = bbox[2] - bbox[0]
                    th = bbox[3] - bbox[1]
                else:
                    break

            w = max(84, min(max_w, tw + pad_w))
            w = ((w + 3) // 4) * 4
            h = max(28, ((th + pad_h + 3) // 4) * 4)
            if len(self._bubble_measure_cache) > 128:
                self._bubble_measure_cache.clear()
            self._bubble_measure_cache[cache_key] = (disp_text, w, h)

        # Keep the entire speech box within the top edge of the canvas
        y = max(h // 2 + 2, y)
        self.canvas.coords(text_id, x, y)

        x0, y0 = int(x - w // 2), int(y - h // 2)
        x1, y1 = int(x + w // 2), int(y + h // 2)

        self.canvas.create_rectangle(x0 + 4, y0, x1 - 4, y1, fill=border_col, outline="", tags=("dyn",))
        self.canvas.create_rectangle(x0, y0 + 4, x1, y1 - 4, fill=border_col, outline="", tags=("dyn",))
        self.canvas.create_rectangle(x0 + 4, y0 + 3, x1 - 4, y1 - 3, fill=inner_border, outline="", tags=("dyn",))
        self.canvas.create_rectangle(x0 + 3, y0 + 4, x1 - 3, y1 - 4, fill=inner_border, outline="", tags=("dyn",))
        self.canvas.create_rectangle(x0 + 6, y0 + 5, x1 - 6, y1 - 5, fill=bg_col, outline="", tags=("dyn",))

        # Seamless downward-pointing pixel arrow (▼) toward the bee
        # Outer dark border steps tapering down
        self.canvas.create_rectangle(x - 8, y1, x + 8, y1 + 3, fill=border_col, outline="", tags=("dyn",))
        self.canvas.create_rectangle(x - 5, y1 + 3, x + 5, y1 + 6, fill=border_col, outline="", tags=("dyn",))
        self.canvas.create_rectangle(x - 2, y1 + 6, x + 2, y1 + 9, fill=border_col, outline="", tags=("dyn",))
        # Inner colored frame steps tapering down
        self.canvas.create_rectangle(x - 6, y1 - 3, x + 6, y1 + 3, fill=inner_border, outline="", tags=("dyn",))
        self.canvas.create_rectangle(x - 3, y1 + 3, x + 3, y1 + 6, fill=inner_border, outline="", tags=("dyn",))
        # Interior parchment fill seamlessly connecting from box interior (y1 - 6) down into arrow
        self.canvas.create_rectangle(x - 4, y1 - 6, x + 4, y1 + 2, fill=bg_col, outline="", tags=("dyn",))
        self.canvas.create_rectangle(x - 2, y1 + 2, x + 2, y1 + 4, fill=bg_col, outline="", tags=("dyn",))

        # Raise the measured text above the bubble background rectangles
        self.canvas.tag_raise(text_id)
        return [
            (x0 + 4, y0, (x1 - 4) - (x0 + 4), y1 - y0),
            (x0, y0 + 4, x1 - x0, (y1 - 4) - (y0 + 4)),
            (x - 8, y1, 16, 3),
            (x - 5, y1 + 3, 10, 3),
            (x - 2, y1 + 6, 4, 3),
        ]

    def run(self):
        if not self._quitting:
            self.root.mainloop()


if __name__ == "__main__":
    if is_wayland_session():
        try:
            from wayland_widget import can_use_wayland_gtk, run_wayland_widget

            if can_use_wayland_gtk():
                run_wayland_widget()
                sys.exit(0)
        except Exception:
            pass
    app = KawaiiBeeWidget()
    app.run()
