#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity Bee Companion (Main GUI Widget)
Modular, cross-platform (Windows & Linux) Pixel-Art Desktop Pet.
"""

import json
import math
import os
import random
import sys
import time
import tkinter as tk
from pathlib import Path

from platform_adapter import (
    TRANSPARENT_KEY,
    acquire_single_instance_lock,
    configure_overlay_window,
    init_dpi_awareness,
    is_pid_alive,
    play_platform_chime,
    set_window_opacity,
    terminate_pid,
)
from pixel_sprites import PIXEL_SCALE, PixelArtRenderer
from state_sync import (
    PID_FILE,
    STATE_DIR,
    STATE_FILE,
    TranscriptWatcher,
    load_config,
    save_config,
    write_state,
)

init_dpi_awareness()

DONE_TO_IDLE_SECONDS = 45.0
IDLE_TO_SLEEP_SECONDS = 90.0

SIZE_PRESETS = {
    "small": {"scale": 3, "width": 200, "height": 180},
    "medium": {"scale": 4, "width": 240, "height": 225},
    "large": {"scale": 5, "width": 290, "height": 270},
}


class KawaiiBeeWidget:
    def __init__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
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
        self.win_x = max(0, min(screen_w - self.width, self.config.get("x", default_x)))
        self.win_y = max(0, min(screen_h - self.height, self.config.get("y", default_y)))

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
        self.last_state_mtime = 0.0
        self.last_mode_change = time.time()
        self.last_interaction = time.time()
        self.sound_enabled = self.config.get("sound", True)
        self.ghost_mode = self.config.get("ghost", False)
        self.auto_launch = self.config.get("auto_launch", True)

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
        self.frame_count = 0

        # Bindings
        self.canvas.bind("<ButtonPress-1>", self.on_left_down)
        self.canvas.bind("<B1-Motion>", self.on_left_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_left_up)
        self.canvas.bind("<Double-Button-1>", self.on_double_click)
        self.canvas.bind("<Button-3>", self.on_right_click)
        self.canvas.bind("<Enter>", self.on_mouse_enter)
        self.canvas.bind("<Leave>", self.on_mouse_leave)
        self.canvas.bind("<Motion>", self.on_mouse_move)

        self._build_context_menu()
        cur_st = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
        if cur_st.get("host_pid", 0) > 0:
            self.host_pid = int(cur_st["host_pid"])
        if not cur_st or cur_st.get("mode", "") in ("quit", "quit_disable", ""):
            write_state("idle", "Idle", swarm_active=False)
        self._apply_opacity()

        self.poll_state()
        self.animate()

    def _claim_single_instance(self):
        # 1. OS Kernel Mutex / flock check: if another bee is already running, exit immediately!
        lock_file = STATE_DIR / "bee.lock"
        if not acquire_single_instance_lock(lock_file):
            sys.exit(0)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    def _persist_config(self):
        self.config["x"] = self.win_x
        self.config["y"] = self.win_y
        self.config["sound"] = self.sound_enabled
        self.config["ghost"] = self.ghost_mode
        self.config["auto_launch"] = self.auto_launch
        self.config["size"] = self.size_name
        save_config(self.config)

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
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.win_x = max(0, min(screen_w - self.width, self.win_x))
        self.win_y = max(0, min(screen_h - self.height, self.win_y))
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
            font=("Consolas", 9, "bold"),
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
            font=("Consolas", 9),
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
            font=("Consolas", 9),
        )
        size_menu.add_command(label="Small (3x)", command=lambda: self.set_size("small"))
        size_menu.add_command(label="Medium (4x)", command=lambda: self.set_size("medium"))
        size_menu.add_command(label="Large (5x)", command=lambda: self.set_size("large"))
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
        new_x = event.x_root - self.drag_start_x
        new_y = event.y_root - self.drag_start_y
        dx = new_x - self.win_x
        dy = new_y - self.win_y
        if abs(dx) > 3 or abs(dy) > 3:
            self.drag_moved = True
        self.vx = dx
        self.vy = dy
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
        self.is_hovered = True
        self._apply_opacity()

    def on_mouse_leave(self, event):
        self.is_hovered = False
        self.last_mouse_x = None
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
        play_platform_chime("poke", self.sound_enabled)

    def feed_honey(self):
        now = time.time()
        self.last_interaction = now
        if self.mode == "sleep":
            self.mode = "idle"
        self.eating_until = now + 3.2
        self.spawn_particles("heart", 6)
        play_platform_chime("honey", self.sound_enabled)

    def toggle_ghost_mode(self):
        self.ghost_mode = not self.ghost_mode
        self._persist_config()
        self._apply_opacity()
        self.show_bubble("Ghost Mode: " + ("ON" if self.ghost_mode else "OFF"), 2.0)

    def toggle_sound(self):
        self.sound_enabled = not self.sound_enabled
        self._persist_config()
        self.show_bubble("Sound: " + ("ON" if self.sound_enabled else "OFF"), 2.0)
        if self.sound_enabled:
            play_platform_chime("poke", True)

    def toggle_auto_launch(self):
        self.auto_launch = not self.auto_launch
        self._persist_config()
        status = "ON" if self.auto_launch else "OFF (/bee)"
        self.show_bubble(f"Auto-Launch: {status}", 3.0)

    def reset_position(self):
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.win_x = screen_w - self.width - 30
        self.win_y = screen_h - self.height - 70
        self.root.geometry(f"+{self.win_x}+{self.win_y}")
        self._persist_config()

    def quit_bee(self, disable_auto: bool = False):
        if disable_auto:
            self.auto_launch = False
            self._persist_config()
        try:
            if PID_FILE.exists():
                PID_FILE.unlink()
        except Exception:
            pass
        self.root.destroy()
        sys.exit(0)

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
                play_platform_chime("needs_input", self.sound_enabled)
            elif new_mode == "done":
                self.show_bubble(f"OK: {self.detail or 'Task completed!'}", 6.0)
                play_platform_chime("done", self.sound_enabled)
            elif new_mode == "error":
                self.show_bubble(f"ERR: {self.detail or 'Error encountered'}", 8.0)
                play_platform_chime("error", self.sound_enabled)
            elif self.detail:
                self.show_bubble(self.detail, 3.5)

    def poll_state(self):
        now = time.time()
        try:
            if STATE_FILE.exists():
                mtime = STATE_FILE.stat().st_mtime
                if mtime > self.last_state_mtime:
                    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                    self.last_state_mtime = mtime
                    if data.get("host_pid", 0) > 0:
                        self.host_pid = int(data["host_pid"])
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
                        return
                    self.set_mode(new_mode, new_detail)
        except Exception:
            pass

        # Auto-close if the Antigravity host process was closed!
        if self.host_pid > 0 and not is_pid_alive(self.host_pid):
            self.quit_bee(disable_auto=False)
            return

        # Live transcript polling
        t_res = self.transcript_watcher.poll(now)
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

        self.root.after(250, self.poll_state)

    # -------------------- 60 FPS Pixel-Art Animation Loop --------------------

    def animate(self):
        now = time.time()
        t = now - self.t0
        self.frame_count += 1
        self.canvas.delete("all")

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

        # Ambient pixel particles
        if eff_mode == "sleep" and self.frame_count % 40 == 0:
            self.spawn_particles("zzz", 1)
        elif eff_mode == "coding" and self.frame_count % 20 == 0:
            self.spawn_particles("sparkle", 1)
        elif eff_mode in ("petting", "eating") and self.frame_count % 16 == 0:
            self.spawn_particles("heart", 1)

        # Wing speed: fast only when actively working or alerting; calm in idle & done!
        if eff_mode in ("needs_input", "coding", "command", "swarm", "dragged"):
            wing_frame = (self.frame_count // 2) % 3
        else:
            wing_frame = (self.frame_count // 5) % 3

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

        # Subtle 1-pixel eye tracking towards mouse cursor
        look_dx, look_dy = 0, 0
        if eff_mode == "reading":
            look_dx = -1 if int(t * 3) % 2 == 0 else 1
        else:
            try:
                mx, my = self.root.winfo_pointerxy()
                rel_x = mx - (self.win_x + bx)
                rel_y = my - (self.win_y + by)
                if rel_x < -45:
                    look_dx = -1
                elif rel_x > 45:
                    look_dx = 1
                if rel_y > 45:
                    look_dy = 1
            except Exception:
                pass

        prop_frame = int(t * 4) % 12
        leg_shift = 1 if self.vx < -3 else (-1 if self.vx > 3 else 0)

        # 1. Ground shadow
        shadow_w = (6 * sc) if bob_y >= 0 else (4 * sc)
        shadow_y = base_cy + 15 * sc
        self.canvas.create_rectangle(
            cx - shadow_w, shadow_y, cx + shadow_w, shadow_y + sc,
            fill="#2C1810", outline=""
        )

        # 2. Orbiting mini bees behind (Swarm mode)
        if eff_mode == "swarm":
            self._draw_mini_bees(bx, by, t, behind=True)

        # 3. Main Pixel-Art Bee Sprite
        sprite = self.renderer.get_bee_sprite(
            eff_mode, wing_frame, eye_state, look_dx, look_dy, prop_frame, leg_shift
        )
        self.canvas.create_image(bx, by, image=sprite, anchor="center")

        # 4. Orbiting mini bees in front (Swarm mode)
        if eff_mode == "swarm":
            self._draw_mini_bees(bx, by, t, behind=False)

        # 5. Pixel-Art Particles
        self._update_and_draw_particles()

        # 6. Retro Pixel-Art RPG Dialogue Bubble (ONLY shown on mouse hover)
        if self.is_hovered:
            txt = self.bubble_text if now < self.bubble_until and self.bubble_text else (self.detail or "Ready")
            offset_top = (16 * sc) if eff_mode in ("needs_input", "error") else (12 * sc)
            bub_y = by - offset_top
            self._draw_pixel_speech_box(bx, max(18, bub_y), txt, eff_mode)

        self.root.after(16, self.animate)

    def _draw_mini_bees(self, bx, by, t, behind=True):
        sc = self.pixel_scale
        mb_sprite = self.renderer.particle_cache[f"mini_bee_{(self.frame_count // 3) % 2}"]
        for i in range(3):
            angle = t * 2.6 + i * (2 * math.pi / 3)
            z = math.sin(angle)
            if (behind and z >= 0) or (not behind and z < 0):
                continue
            mx = int((bx + math.cos(angle) * (15.5 * sc)) // sc) * sc
            my = int((by + z * (5.0 * sc)) // sc) * sc
            self.canvas.create_image(mx, my, image=mb_sprite, anchor="center")

    def _update_and_draw_particles(self):
        alive = []
        for p in self.particles:
            p["life"] += 1
            if p["life"] < p["max_life"]:
                p["x"] += p["vx"]
                p["y"] += p["vy"]
                spr = self.renderer.particle_cache.get(p["kind"])
                if spr:
                    px = int(p["x"] // 2) * 2
                    py = int(p["y"] // 2) * 2
                    self.canvas.create_image(px, py, image=spr, anchor="center")
                alive.append(p)
        self.particles = alive

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

        max_w = self.width - 8
        w = max(108, min(max_w, len(text) * 7 + 24))
        w = (w // 4) * 4
        h = 28
        x0, y0 = int(x - w // 2), int(y - h // 2)
        x1, y1 = int(x + w // 2), int(y + h // 2)

        self.canvas.create_rectangle(x0 + 4, y0, x1 - 4, y1, fill=border_col, outline="")
        self.canvas.create_rectangle(x0, y0 + 4, x1, y1 - 4, fill=border_col, outline="")
        self.canvas.create_rectangle(x0 + 4, y0 + 3, x1 - 4, y1 - 3, fill=inner_border, outline="")
        self.canvas.create_rectangle(x0 + 3, y0 + 4, x1 - 3, y1 - 4, fill=inner_border, outline="")
        self.canvas.create_rectangle(x0 + 6, y0 + 5, x1 - 6, y1 - 5, fill=bg_col, outline="")

        # Seamless downward-pointing pixel arrow (▼) toward the bee
        # Outer dark border steps tapering down
        self.canvas.create_rectangle(x - 8, y1, x + 8, y1 + 3, fill=border_col, outline="")
        self.canvas.create_rectangle(x - 5, y1 + 3, x + 5, y1 + 6, fill=border_col, outline="")
        self.canvas.create_rectangle(x - 2, y1 + 6, x + 2, y1 + 9, fill=border_col, outline="")
        # Inner colored frame steps tapering down
        self.canvas.create_rectangle(x - 6, y1 - 3, x + 6, y1 + 3, fill=inner_border, outline="")
        self.canvas.create_rectangle(x - 3, y1 + 3, x + 3, y1 + 6, fill=inner_border, outline="")
        # Interior parchment fill seamlessly connecting from box interior (y1 - 6) down into arrow
        self.canvas.create_rectangle(x - 4, y1 - 6, x + 4, y1 + 2, fill=bg_col, outline="")
        self.canvas.create_rectangle(x - 2, y1 + 2, x + 2, y1 + 4, fill=bg_col, outline="")

        self.canvas.create_text(
            x, y,
            text=text,
            fill=text_col,
            font=("Consolas", 9, "bold"),
        )

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = KawaiiBeeWidget()
    app.run()
