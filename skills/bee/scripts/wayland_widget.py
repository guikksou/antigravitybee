#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity Bee Companion (Linux Wayland / Hyprland 32-bit ARGB Widget)
Uses GTK3 + Cairo with a 32-bit RGBA visual (XWayland override-redirect when
DISPLAY is present, or GtkLayerShell / native Wayland RGBA fallback) for true
per-pixel alpha=0 transparency and 60-FPS pixel-art rendering.
"""

from __future__ import annotations

import math
import os
import random
import signal
import sys
import time
from collections import OrderedDict
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from platform_adapter import (
    acquire_single_instance_lock,
    is_pid_alive,
)
from pixel_sprites import (
    PALETTE,
    PALETTE_RGBA,
    PixelArtRenderer,
    get_particle_color_spans,
    hex_to_rgba,
)
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

DONE_TO_IDLE_SECONDS = 45.0
IDLE_TO_SLEEP_SECONDS = 90.0
MAX_PARTICLES = 40
UI_FONT_FAMILY = "DejaVu Sans Mono"

SIZE_PRESETS = {
    "small": {"scale": 3, "width": 200, "height": 180},
    "medium": {"scale": 4, "width": 240, "height": 225},
    "large": {"scale": 5, "width": 290, "height": 270},
    "xlarge": {"scale": 7, "width": 390, "height": 360},
}

_GTK_LOADED = False
_GTK_AVAILABLE = False
gi = None
Gtk = None
Gdk = None
GLib = None
cairo = None
GtkLayerShell = None

_RGBA_CACHE: dict[str, tuple[float, float, float, float]] = {
    PALETTE[k]: rgba for k, rgba in PALETTE_RGBA.items() if k != "."
}


def _color_rgba(hex_col: str) -> tuple[float, float, float, float]:
    cached = _RGBA_CACHE.get(hex_col)
    if cached is None:
        cached = hex_to_rgba(hex_col)
        _RGBA_CACHE[hex_col] = cached
    return cached


def _configure_gdk_backend() -> None:
    """Force X11/XWayland when DISPLAY is set unless native Wayland is requested."""
    if "gi.repository.Gtk" in sys.modules:
        return
    native_req = os.environ.get("AGY_BEE_NATIVE_WAYLAND", "").strip().lower()
    if native_req in ("1", "true", "yes"):
        os.environ["GDK_BACKEND"] = "wayland"
    elif os.environ.get("DISPLAY"):
        os.environ["GDK_BACKEND"] = "x11"


def _ensure_gtk_modules() -> bool:
    """Lazily import gi + Gtk 3.0 + Gdk + GLib + cairo (and optional GtkLayerShell)."""
    global _GTK_LOADED, _GTK_AVAILABLE, gi, Gtk, Gdk, GLib, cairo, GtkLayerShell
    if _GTK_LOADED:
        return _GTK_AVAILABLE
    _GTK_LOADED = True
    try:
        _configure_gdk_backend()
        import gi as _gi
        _gi.require_version("Gdk", "3.0")
        _gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk as _Gtk, Gdk as _Gdk, GLib as _GLib
        import cairo as _cairo

        gi = _gi
        Gtk = _Gtk
        Gdk = _Gdk
        GLib = _GLib
        cairo = _cairo

        try:
            _gi.require_version("GtkLayerShell", "0.1")
            from gi.repository import GtkLayerShell as _GtkLayerShell
            GtkLayerShell = _GtkLayerShell
        except Exception:
            GtkLayerShell = None

        _GTK_AVAILABLE = True
    except Exception:
        _GTK_AVAILABLE = False
    return _GTK_AVAILABLE


def can_use_wayland_gtk() -> bool:
    """Return True if PyGObject GTK 3.0 and PyCairo can be imported."""
    return _ensure_gtk_modules()


class KawaiiBeeWaylandWidget:
    """32-bit ARGB GTK3/Cairo desktop companion widget for Wayland & Hyprland."""

    def __init__(self, offscreen: bool = False):
        if not _ensure_gtk_modules():
            raise RuntimeError("PyGObject GTK 3.0 and PyCairo are required for wayland_widget")

        self.offscreen = bool(offscreen)
        self._quitting = False
        self._poll_source_id: int | None = None
        self._anim_source_id: int | None = None
        self._leave_source_id: int | None = None
        self._using_layer_shell = False

        self.win = None
        self.drawing_area = None
        self.menu = None

        has_display = (not self.offscreen) and (Gdk.Display.get_default() is not None)
        if not self.offscreen:
            STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._claim_single_instance()

        self.config = load_config()
        self.size_name = str(self.config.get("size", "medium")).lower()
        if self.size_name not in SIZE_PRESETS:
            self.size_name = "medium"
        preset = SIZE_PRESETS[self.size_name]
        self.pixel_scale = preset["scale"]
        self.width = preset["width"]
        self.height = preset["height"]

        self.renderer = PixelArtRenderer(scale=self.pixel_scale, use_tk=False)
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
        self.ghost_mode = bool(self.config.get("ghost", False))
        self.auto_launch = bool(self.config.get("auto_launch", True))
        self.current_opacity = 0.45 if self.ghost_mode else 1.0

        # Caching & Animation state
        self._cached_look_dx = 0
        self._cached_look_dy = 0
        self._last_pointer_poll = 0.0
        self._bubble_measure_cache: dict[tuple[str, int, int], tuple[str, int, int]] = {}
        self._surface_cache: OrderedDict[tuple, object] = OrderedDict()
        self._last_render_key: tuple | None = None
        self._last_state_data: dict | None = None
        self._drag_moved_this_frame = False
        self._last_local_pointer: tuple[float, float] | None = None

        # Mouse & physics state
        self.is_dragging = False
        self.drag_start_x = 0.0
        self.drag_start_y = 0.0
        self.drag_local_x = 0.0
        self.drag_local_y = 0.0
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

        self.particles: list[dict] = []
        self.next_blink = time.time() + random.uniform(2.5, 5.0)
        self.blink_until = 0.0

        self.t0 = time.time()
        self._last_anim_time = self.t0
        self._last_ambient_particle_time = self.t0
        self.frame_count = 0
        self._frame_snapshot: dict | None = None

        if has_display:
            self._init_window()
            mon_x, mon_y, mon_w, mon_h = self._get_primary_monitor_geometry()
            default_x = mon_x + mon_w - self.width - 30
            default_y = mon_y + mon_h - self.height - 70
            self.win_x, self.win_y = self._clamp_position(
                self.config.get("x", default_x),
                self.config.get("y", default_y),
            )
            self._build_context_menu()
            self._move_window(self.win_x, self.win_y)
            self.win.show_all()
            self._move_window(self.win_x, self.win_y)
        else:
            self.win_x = int(self.config.get("x", 1650))
            self.win_y = int(self.config.get("y", 785))

        if not self.offscreen:
            cur_st = read_state()
            self._last_state_data = cur_st
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
        self._frame_snapshot = self._compute_frame_state(time.time(), advance=False)
        self._last_render_key = self._frame_snapshot.get("render_key")

        if has_display and not self._quitting:
            self._install_signal_handlers()
            self.poll_state()
            if not self._quitting:
                self._poll_source_id = GLib.timeout_add(250, self.poll_state)
                self._anim_source_id = GLib.timeout_add(
                    self._next_anim_interval_ms(time.time()),
                    self.animate,
                )

    # -------------------- Backend & Geometry Helpers --------------------

    @staticmethod
    def _backend_name() -> str:
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                return type(display).__name__
        except Exception:
            pass
        return ""

    def _is_x11_backend(self) -> bool:
        return "X11" in self._backend_name()

    def _is_wayland_backend(self) -> bool:
        return "Wayland" in self._backend_name()

    def _init_window(self) -> None:
        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title("Antigravity Bee")
        self.win.set_app_paintable(True)
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_accept_focus(False)
        self.win.set_keep_above(True)
        self.win.stick()

        screen = self.win.get_screen()
        if screen is not None:
            visual = screen.get_rgba_visual()
            if visual is not None:
                self.win.set_visual(visual)

        if (
            self._is_wayland_backend()
            and GtkLayerShell is not None
            and GtkLayerShell.is_supported()
        ):
            GtkLayerShell.init_for_window(self.win)
            GtkLayerShell.set_layer(self.win, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_namespace(self.win, "antigravity-bee")
            GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.TOP, True)
            GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.LEFT, True)
            GtkLayerShell.set_exclusive_zone(self.win, -1)
            GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.NONE)
            self._using_layer_shell = True

        self.win.set_size_request(self.width, self.height)
        try:
            geom = Gdk.Geometry()
            geom.min_width = self.width
            geom.max_width = self.width
            geom.min_height = self.height
            geom.max_height = self.height
            self.win.set_geometry_hints(
                None,
                geom,
                Gdk.WindowHints.MIN_SIZE | Gdk.WindowHints.MAX_SIZE,
            )
        except Exception:
            pass
        self.win.resize(self.width, self.height)

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_size_request(self.width, self.height)
        self.drawing_area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.drawing_area.connect("draw", self.on_draw)
        self.drawing_area.connect("button-press-event", self.on_button_press)
        self.drawing_area.connect("button-release-event", self.on_button_release)
        self.drawing_area.connect("motion-notify-event", self.on_motion_notify)
        self.drawing_area.connect("enter-notify-event", self.on_enter_notify)
        self.drawing_area.connect("leave-notify-event", self.on_leave_notify)

        self.win.connect("realize", self._on_realize)
        self.win.connect("destroy", lambda *_: self.quit_bee(disable_auto=False))
        self.win.add(self.drawing_area)

    def _on_realize(self, _widget) -> None:
        if self.win is None:
            return
        gdk_win = self.win.get_window()
        if gdk_win is not None and self._is_x11_backend():
            try:
                gdk_win.set_override_redirect(True)
                gdk_win.raise_()
            except Exception:
                pass
        self._move_window(self.win_x, self.win_y)

    def _move_window(self, x: int, y: int) -> None:
        if self.win is None:
            return
        if self._using_layer_shell and GtkLayerShell is not None:
            GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.LEFT, int(x))
            GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.TOP, int(y))
        else:
            self.win.move(int(x), int(y))

    def _install_signal_handlers(self) -> None:
        if hasattr(GLib, "unix_signal_add"):
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    GLib.unix_signal_add(
                        GLib.PRIORITY_DEFAULT,
                        sig,
                        lambda: (self.quit_bee(disable_auto=False), False)[1],
                    )
                except Exception:
                    pass

    def _get_primary_monitor_geometry(self) -> tuple[int, int, int, int]:
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                mon = display.get_primary_monitor()
                if mon is None and display.get_n_monitors() > 0:
                    mon = display.get_monitor(0)
                if mon is not None:
                    geo = mon.get_geometry()
                    return int(geo.x), int(geo.y), int(geo.width), int(geo.height)
        except Exception:
            pass
        if self.win is not None:
            try:
                screen = self.win.get_screen()
                if screen is not None:
                    return 0, 0, int(screen.get_width()), int(screen.get_height())
            except Exception:
                pass
        return 0, 0, 1920, 1080

    def _get_virtual_screen_bounds(self) -> tuple[int, int, int, int]:
        """Return (min_x, min_y, max_x, max_y) across all connected monitors."""
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                n = display.get_n_monitors()
                if n > 0:
                    geos = [display.get_monitor(i).get_geometry() for i in range(n)]
                    min_x = min(g.x for g in geos)
                    min_y = min(g.y for g in geos)
                    max_x = max(g.x + g.width for g in geos)
                    max_y = max(g.y + g.height for g in geos)
                    if max_x > min_x and max_y > min_y:
                        return int(min_x), int(min_y), int(max_x), int(max_y)
        except Exception:
            pass
        if self.win is not None:
            try:
                screen = self.win.get_screen()
                if screen is not None:
                    return 0, 0, int(screen.get_width()), int(screen.get_height())
            except Exception:
                pass
        return 0, 0, 1920, 1080

    def _clamp_position(self, x: int | float, y: int | float) -> tuple[int, int]:
        vx0, vy0, vx1, vy1 = self._get_virtual_screen_bounds()
        cx = max(vx0, min(vx1 - self.width, int(x)))
        cy = max(vy0, min(vy1 - self.height, int(y)))
        return cx, cy

    def _get_pointer_xy(self) -> tuple[int, int] | None:
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                seat = display.get_default_seat()
                if seat is not None:
                    pointer = seat.get_pointer()
                    if pointer is not None:
                        _screen, px, py = pointer.get_position()
                        if self._is_wayland_backend() and px == 0 and py == 0:
                            if self.is_hovered and self._last_local_pointer is not None:
                                lx, ly = self._last_local_pointer
                                return self.win_x + int(lx), self.win_y + int(ly)
                            return None
                        return int(px), int(py)
        except Exception:
            pass
        return None

    # -------------------- Single Instance & Persistence --------------------

    def _claim_single_instance(self) -> None:
        lock_file = STATE_DIR / "bee.lock"
        if not acquire_single_instance_lock(lock_file):
            sys.exit(0)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    def _persist_config(self) -> None:
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

    def set_size(self, size_name: str) -> None:
        size_name = size_name.lower()
        if size_name not in SIZE_PRESETS:
            return
        self.size_name = size_name
        preset = SIZE_PRESETS[size_name]
        self.pixel_scale = preset["scale"]
        self.width = preset["width"]
        self.height = preset["height"]
        self.renderer.set_scale(self.pixel_scale)
        self._bubble_measure_cache.clear()
        self._surface_cache.clear()
        self._last_render_key = None
        self.win_x, self.win_y = self._clamp_position(self.win_x, self.win_y)
        if self.drawing_area is not None:
            self.drawing_area.set_size_request(self.width, self.height)
        if self.win is not None:
            self.win.set_size_request(self.width, self.height)
            try:
                geom = Gdk.Geometry()
                geom.min_width = self.width
                geom.max_width = self.width
                geom.min_height = self.height
                geom.max_height = self.height
                self.win.set_geometry_hints(
                    None,
                    geom,
                    Gdk.WindowHints.MIN_SIZE | Gdk.WindowHints.MAX_SIZE,
                )
            except Exception:
                pass
            self.win.resize(self.width, self.height)
            gdk_win = self.win.get_window()
            if gdk_win is not None:
                try:
                    gdk_win.move_resize(self.win_x, self.win_y, self.width, self.height)
                except Exception:
                    try:
                        gdk_win.resize(self.width, self.height)
                    except Exception:
                        pass
            self._move_window(self.win_x, self.win_y)
        self._frame_snapshot = self._compute_frame_state(time.time(), advance=False)
        if self.drawing_area is not None:
            self.drawing_area.queue_draw()
        if not self.offscreen:
            self._persist_config()

    # -------------------- Context Menu --------------------

    def _build_context_menu(self) -> None:
        self.menu = Gtk.Menu()
        self.menu.get_style_context().add_class("agy-bee-menu")

        screen = self.win.get_screen() if self.win is not None else None
        if screen is not None:
            try:
                provider = Gtk.CssProvider()
                provider.load_from_data(
                    b"""
                    .agy-bee-menu, .agy-bee-menu menu {
                        background-color: #2C1810;
                        color: #FFF394;
                        border: 1px solid #FFCC33;
                        font-family: "DejaVu Sans Mono", monospace;
                        font-size: 9pt;
                    }
                    .agy-bee-menu menuitem {
                        color: #FFF394;
                        padding: 4px 10px;
                    }
                    .agy-bee-menu menuitem:hover {
                        background-color: #FFCC33;
                        color: #2C1810;
                    }
                    .agy-bee-menu separator {
                        background-color: #5C3422;
                        min-height: 1px;
                    }
                    """
                )
                Gtk.StyleContext.add_provider_for_screen(
                    screen,
                    provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
                )
            except Exception:
                pass

        def add_item(parent_menu, label: str, callback) -> None:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda *_: callback())
            parent_menu.append(item)

        add_item(self.menu, "Feed Honey", self.feed_honey)
        add_item(self.menu, "Poke / Wave", self.trigger_poke)

        costume_item = Gtk.MenuItem(label="Preview Mode")
        costume_menu = Gtk.Menu()
        costume_menu.get_style_context().add_class("agy-bee-menu")
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
            add_item(
                costume_menu,
                label,
                lambda c=m_code, d=m_det: self.set_mode(c, d, manual=True),
            )
        costume_item.set_submenu(costume_menu)
        self.menu.append(costume_item)

        size_item = Gtk.MenuItem(label="Widget Size")
        size_menu = Gtk.Menu()
        size_menu.get_style_context().add_class("agy-bee-menu")
        for label, sz in (
            ("Small (3x)", "small"),
            ("Medium (4x)", "medium"),
            ("Large (5x)", "large"),
            ("Extra Large (7x)", "xlarge"),
        ):
            add_item(size_menu, label, lambda s=sz: self.set_size(s))
        size_item.set_submenu(size_menu)
        self.menu.append(size_item)

        self.menu.append(Gtk.SeparatorMenuItem())
        add_item(self.menu, "Ghost Mode (On/Off)", self.toggle_ghost_mode)
        add_item(self.menu, "Auto-Launch (On/Off)", self.toggle_auto_launch)
        add_item(self.menu, "Reset Position", self.reset_position)
        self.menu.append(Gtk.SeparatorMenuItem())
        add_item(
            self.menu,
            "Close & Disable Auto-Launch",
            lambda: self.quit_bee(disable_auto=True),
        )
        add_item(
            self.menu,
            "Close",
            lambda: self.quit_bee(disable_auto=False),
        )
        self.menu.show_all()

    # -------------------- Mouse Interactions --------------------

    def on_button_press(self, _widget, event) -> bool:
        if event.button == 1:
            if event.type == Gdk.EventType._2BUTTON_PRESS:
                self.feed_honey()
                return True
            self.is_dragging = True
            self.drag_moved = False
            self.drag_start_x = event.x_root - self.win_x
            self.drag_start_y = event.y_root - self.win_y
            self.drag_local_x = event.x
            self.drag_local_y = event.y
            self.last_interaction = time.time()
            return True
        if event.button == 3 and self.menu is not None:
            self.menu.popup_at_pointer(event)
            return True
        return False

    def on_button_release(self, _widget, event) -> bool:
        if event.button != 1:
            return False
        was_dragging = self.is_dragging
        self.is_dragging = False
        self.vx = 0.0
        self.vy = 0.0
        if not self.offscreen:
            self._persist_config()

        if was_dragging and not self.drag_moved:
            if self.mode in ("needs_input", "done", "error"):
                self.set_mode("idle", "Idle", manual=True)
                if not self.offscreen:
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
        return True

    def on_motion_notify(self, _widget, event) -> bool:
        self._last_local_pointer = (event.x, event.y)
        if self.is_dragging:
            if self._using_layer_shell:
                target_x = self.win_x + (event.x - self.drag_local_x)
                target_y = self.win_y + (event.y - self.drag_local_y)
            else:
                target_x = event.x_root - self.drag_start_x
                target_y = event.y_root - self.drag_start_y
            new_x, new_y = self._clamp_position(target_x, target_y)
            dx = new_x - self.win_x
            dy = new_y - self.win_y
            if abs(dx) > 3 or abs(dy) > 3:
                self.drag_moved = True
            self.vx = dx
            self.vy = dy
            self._drag_moved_this_frame = True
            self.win_x = new_x
            self.win_y = new_y
            self._move_window(self.win_x, self.win_y)
            return True

        cx, cy = self.width // 2, int(self.height * 0.54)
        rx, ry = 15 * self.pixel_scale, 14 * self.pixel_scale
        if (cx - rx) <= event.x <= (cx + rx) and (cy - ry) <= event.y <= (cy + ry):
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
        return False

    def on_enter_notify(self, _widget, event) -> bool:
        self._last_local_pointer = (event.x, event.y)
        if self._leave_source_id is not None:
            try:
                GLib.source_remove(self._leave_source_id)
            except Exception:
                pass
            self._leave_source_id = None
        self.is_hovered = True
        self._last_render_key = None
        self._apply_opacity()
        if self.drawing_area is not None:
            self.drawing_area.queue_draw()
        return False

    def on_leave_notify(self, _widget, _event) -> bool:
        if self._leave_source_id is not None:
            try:
                GLib.source_remove(self._leave_source_id)
            except Exception:
                pass
        self._leave_source_id = GLib.timeout_add(120, self._confirm_mouse_leave)
        return False

    def _confirm_mouse_leave(self) -> bool:
        self._leave_source_id = None
        if self._quitting:
            return False
        pt = self._get_pointer_xy()
        if pt is not None and not self._is_wayland_backend():
            mx, my = pt
            cx = self.win_x + self.width // 2
            base_cy = self.win_y + int(self.height * 0.54)
            rx = 16 * self.pixel_scale
            ry = 18 * self.pixel_scale
            if abs(mx - cx) <= rx and abs(my - base_cy) <= ry:
                return False
        self.is_hovered = False
        self.last_mouse_x = None
        self.pet_score = 0.0
        self._last_render_key = None
        self._apply_opacity()
        if self.drawing_area is not None:
            self.drawing_area.queue_draw()
        return False

    # -------------------- Interactive Actions --------------------

    def trigger_petting(self) -> None:
        now = time.time()
        self.last_interaction = now
        if self.mode == "sleep":
            self.mode = "idle"
        self.petting_until = now + 2.2
        self.spawn_particles("heart", 5)

    def trigger_poke(self) -> None:
        now = time.time()
        self.last_interaction = now
        if self.mode == "sleep":
            self.mode = "idle"
        self.poke_until = now + 0.6
        self.spawn_particles("sparkle", 5)

    def feed_honey(self) -> None:
        now = time.time()
        self.last_interaction = now
        if self.mode == "sleep":
            self.mode = "idle"
        self.eating_until = now + 3.2
        self.spawn_particles("heart", 6)

    def toggle_ghost_mode(self) -> None:
        self.ghost_mode = not self.ghost_mode
        if not self.offscreen:
            self._persist_config()
        self._apply_opacity()
        self.show_bubble("Ghost Mode: " + ("ON" if self.ghost_mode else "OFF"), 2.0)

    def toggle_auto_launch(self) -> None:
        self.auto_launch = not self.auto_launch
        if not self.offscreen:
            self._persist_config()
        status = "ON" if self.auto_launch else "OFF (/bee)"
        self.show_bubble(f"Auto-Launch: {status}", 3.0)

    def reset_position(self) -> None:
        mon_x, mon_y, mon_w, mon_h = self._get_primary_monitor_geometry()
        self.win_x, self.win_y = self._clamp_position(
            mon_x + mon_w - self.width - 30,
            mon_y + mon_h - self.height - 70,
        )
        self._move_window(self.win_x, self.win_y)
        if not self.offscreen:
            self._persist_config()

    def quit_bee(self, disable_auto: bool = False) -> None:
        if self._quitting:
            return
        self._quitting = True
        if disable_auto:
            self.auto_launch = False
            if not self.offscreen:
                self._persist_config()
        for attr in ("_poll_source_id", "_anim_source_id", "_leave_source_id"):
            source_id = getattr(self, attr, None)
            if source_id is not None:
                try:
                    GLib.source_remove(source_id)
                except Exception:
                    pass
                setattr(self, attr, None)
        if not self.offscreen:
            try:
                if PID_FILE.exists() and not PID_FILE.is_symlink():
                    PID_FILE.unlink()
            except Exception:
                pass
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None
        try:
            if Gtk.main_level() > 0:
                Gtk.main_quit()
        except Exception:
            pass

    def _apply_opacity(self) -> None:
        if self.is_hovered or self.mode in ("needs_input", "done", "error"):
            target = 1.0
        elif self.ghost_mode:
            target = 0.45
        else:
            target = 1.0
        if target != self.current_opacity:
            self.current_opacity = target
            self._last_render_key = None
            if self.drawing_area is not None:
                self.drawing_area.queue_draw()

    def show_bubble(self, text: str, duration: float = 3.0) -> None:
        self.bubble_text = text
        self.bubble_until = time.time() + duration

    def spawn_particles(self, kind: str, count: int = 5) -> None:
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

    def set_mode(self, new_mode: str, new_detail: str, manual: bool = False) -> None:
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

    def poll_state(self) -> bool:
        if self._quitting:
            return False
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
                            return False
                        if new_mode == "quit_disable":
                            self.quit_bee(disable_auto=True)
                            return False
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
            return False

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

        return not self._quitting

    # -------------------- Adaptive Animation & 32-bit ARGB Cairo Rendering --------------------

    def _effective_mode(self, now: float) -> str:
        if self.is_dragging:
            return "dragged"
        if now < self.eating_until:
            return "eating"
        if now < self.petting_until:
            return "petting"
        return self.mode

    def _next_anim_interval_ms(self, now: float) -> int:
        eff_mode = self._effective_mode(now)
        if (
            eff_mode in ("needs_input", "dragged", "swarm", "eating", "petting")
            or now < self.poke_until
            or len(self.particles) > 0
        ):
            return 42
        if eff_mode == "sleep":
            return 166
        return 83

    def _compute_frame_state(self, now: float, advance: bool = True) -> dict:
        t = now - self.t0
        dt_60 = 0.0
        if advance:
            dt = max(0.0, min(now - self._last_anim_time, 0.25))
            self._last_anim_time = now
            dt_60 = dt * 60.0
            self.frame_count += 1

        eff_mode = self._effective_mode(now)

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
            bob_y = int(math.sin(t * 3.5) * 2) * sc
            bob_x = 0

        if now < self.poke_until:
            prog = (self.poke_until - now) / 0.6
            bob_y -= int(math.sin(prog * math.pi) * 4) * sc

        bx = cx + bob_x
        by = base_cy + bob_y

        if advance:
            ambient_kind = None
            ambient_interval = None
            if eff_mode == "sleep":
                ambient_kind = "zzz"
                ambient_interval = 40.0 / 60.0
            elif eff_mode == "coding":
                ambient_kind = "sparkle"
                ambient_interval = 20.0 / 60.0
            elif eff_mode in ("petting", "eating"):
                ambient_kind = "heart"
                ambient_interval = 16.0 / 60.0
            if ambient_kind is not None and (now - self._last_ambient_particle_time) >= ambient_interval:
                self._last_ambient_particle_time = now
                self.spawn_particles(ambient_kind, 1)

        if eff_mode in ("needs_input", "coding", "command", "swarm", "dragged"):
            wing_frame = int(t * 30.0) % 3
        else:
            wing_frame = int(t * 12.0) % 3

        if advance and now >= self.next_blink:
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
            eye_state = "normal"

        if eff_mode == "reading":
            look_dx = -1 if int(t * 3) % 2 == 0 else 1
            look_dy = 0
        elif eff_mode == "sleep":
            look_dx = 0
            look_dy = 0
        else:
            if advance and (now - self._last_pointer_poll) >= 0.20:
                self._last_pointer_poll = now
                pt = self._get_pointer_xy()
                if pt is not None:
                    mx, my = pt
                    rel_x = mx - (self.win_x + bx)
                    rel_y = my - (self.win_y + by)
                    self._cached_look_dx = -1 if rel_x < -45 else (1 if rel_x > 45 else 0)
                    self._cached_look_dy = 1 if rel_y > 45 else 0
            look_dx = self._cached_look_dx
            look_dy = self._cached_look_dy

        if advance:
            if not self._drag_moved_this_frame:
                self.vx = 0.0
                self.vy = 0.0
            self._drag_moved_this_frame = False

        prop_frame = int(t * 4) % 12
        leg_shift = 1 if self.vx < -3 else (-1 if self.vx > 3 else 0)

        shadow_w = (6 * sc) if bob_y >= 0 else (4 * sc)
        shadow_y = base_cy + 15 * sc

        mini_bee_positions = []
        if eff_mode == "swarm":
            for i in range(3):
                angle = t * 2.6 + i * (2 * math.pi / 3)
                z = math.sin(angle)
                mx = int((bx + math.cos(angle) * (15.5 * sc)) // sc) * sc
                my = int((by + z * (5.0 * sc)) // sc) * sc
                mini_bee_positions.append((mx, my, z))

        if advance:
            alive = []
            for p in self.particles:
                p["life"] += dt_60
                if p["life"] < p["max_life"]:
                    p["x"] += p["vx"] * dt_60
                    p["y"] += p["vy"] * dt_60
                    alive.append(p)
            self.particles = alive

        mb_frame = int(t * 20.0) % 2
        norm_key = self.renderer._normalize_key(
            eff_mode, wing_frame, eye_state, look_dx, look_dy, prop_frame, leg_shift
        )
        hover_txt = (
            (self.bubble_text if now < self.bubble_until and self.bubble_text else (self.detail or "Ready"))
            if self.is_hovered
            else ""
        )
        hover_key = (self.is_hovered, hover_txt, eff_mode if self.is_hovered else "")
        particle_key = tuple((p["kind"], int(p["x"] // 2) * 2, int(p["y"] // 2) * 2) for p in self.particles)
        mini_key = (mb_frame, tuple(mini_bee_positions)) if eff_mode == "swarm" else ()
        render_key = (
            sc,
            bx,
            by,
            norm_key,
            shadow_w,
            shadow_y,
            self.current_opacity,
            hover_key,
            mini_key,
            particle_key,
        )

        return {
            "now": now,
            "eff_mode": eff_mode,
            "sc": sc,
            "cx": cx,
            "bx": bx,
            "by": by,
            "wing_frame": wing_frame,
            "eye_state": eye_state,
            "look_dx": look_dx,
            "look_dy": look_dy,
            "prop_frame": prop_frame,
            "leg_shift": leg_shift,
            "norm_key": norm_key,
            "shadow_w": shadow_w,
            "shadow_y": shadow_y,
            "mini_bee_positions": mini_bee_positions,
            "mb_frame": mb_frame,
            "render_key": render_key,
        }

    def animate(self) -> bool:
        self._anim_source_id = None
        if self._quitting:
            return False
        now = time.time()
        snap = self._compute_frame_state(now, advance=True)
        self._frame_snapshot = snap
        render_key = snap["render_key"]
        if render_key != self._last_render_key:
            self._last_render_key = render_key
            if self.drawing_area is not None:
                self.drawing_area.queue_draw()
        next_ms = self._next_anim_interval_ms(now)
        self._anim_source_id = GLib.timeout_add(next_ms, self.animate)
        return False

    def _draw_spans_cairo(
        self,
        cr,
        spans: list[tuple[int, int, int, str]],
        x0: int,
        y0: int,
        scale: int,
        opacity: float,
    ) -> None:
        last_col = None
        for x_start, y, span_len, hex_col in spans:
            if hex_col != last_col:
                r, g, b, a = _color_rgba(hex_col)
                cr.set_source_rgba(r, g, b, a * opacity)
                last_col = hex_col
            cr.rectangle(x0 + x_start * scale, y0 + y * scale, span_len * scale, scale)
            cr.fill()

    def _get_or_render_surface(
        self,
        cache_key: tuple,
        spans: list[tuple[int, int, int, str]],
        width: int,
        height: int,
        scale: int,
    ):
        surf = self._surface_cache.get(cache_key)
        if surf is not None:
            self._surface_cache.move_to_end(cache_key)
            return surf
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        scr = cairo.Context(surf)
        scr.set_antialias(cairo.ANTIALIAS_NONE)
        self._draw_spans_cairo(scr, spans, 0, 0, scale, 1.0)
        if len(self._surface_cache) >= 256:
            self._surface_cache.popitem(last=False)
        self._surface_cache[cache_key] = surf
        return surf

    def _draw_mini_bees_cairo(
        self,
        cr,
        mini_bee_positions: list[tuple[int, int, float]],
        mb_frame: int,
        p_scale: int,
        opacity: float,
        behind: bool,
    ) -> None:
        spans, gw, gh = get_particle_color_spans(f"mini_bee_{mb_frame}")
        if not spans:
            return
        pw = gw * p_scale
        ph = gh * p_scale
        surf = self._get_or_render_surface(
            ("particle", p_scale, f"mini_bee_{mb_frame}"),
            spans,
            pw,
            ph,
            p_scale,
        )
        for mx, my, z in mini_bee_positions:
            if (behind and z >= 0) or (not behind and z < 0):
                continue
            cr.set_source_surface(surf, mx - pw // 2, my - ph // 2)
            if opacity < 0.999:
                cr.paint_with_alpha(opacity)
            else:
                cr.paint()

    def _draw_particles_cairo(self, cr, p_scale: int, opacity: float) -> None:
        for p in self.particles:
            kind = p["kind"]
            spans, gw, gh = get_particle_color_spans(kind)
            if not spans:
                continue
            pw = gw * p_scale
            ph = gh * p_scale
            surf = self._get_or_render_surface(
                ("particle", p_scale, kind),
                spans,
                pw,
                ph,
                p_scale,
            )
            px = int(p["x"] // 2) * 2 - pw // 2
            py = int(p["y"] // 2) * 2 - ph // 2
            cr.set_source_surface(surf, px, py)
            if opacity < 0.999:
                cr.paint_with_alpha(opacity)
            else:
                cr.paint()

    def _draw_pixel_speech_box_cairo(
        self,
        cr,
        x: int,
        y: int,
        text: str,
        eff_mode: str,
        opacity: float,
    ) -> None:
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

        font_px = max(12, int(self.pixel_scale * 3.0))
        cr.select_font_face(UI_FONT_FAMILY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(font_px)

        cache_key = (text, self.width, font_px)
        cached = self._bubble_measure_cache.get(cache_key)
        if cached is not None:
            disp_text, w, h = cached
            ext = cr.text_extents(disp_text)
        else:
            disp_text = text
            ext = cr.text_extents(disp_text)
            tw = max(ext.width, ext.x_advance)
            th = ext.height
            # Inner white box has 6px border on left and 6px border on right (12px total)
            # plus 10px comfortable horizontal padding on each side = 32px total padding
            pad_w = 32
            pad_h = 20
            max_w = self.width - 8
            while (tw + pad_w > max_w or len(disp_text) > 26) and len(disp_text) > 5:
                if disp_text.endswith(".."):
                    disp_text = disp_text[:-3] + ".."
                else:
                    disp_text = disp_text[:-2] + ".."
                ext = cr.text_extents(disp_text)
                tw = max(ext.width, ext.x_advance)
                th = ext.height

            w = max(84, min(max_w, int(math.ceil(tw)) + pad_w))
            w = ((w + 3) // 4) * 4
            h = max(28, ((int(math.ceil(th)) + pad_h + 3) // 4) * 4)
            if len(self._bubble_measure_cache) > 128:
                self._bubble_measure_cache.clear()
            self._bubble_measure_cache[cache_key] = (disp_text, w, h)

        y = max(h // 2 + 2, y)
        x0, y0 = int(x - w // 2), int(y - h // 2)
        x1, y1 = int(x + w // 2), int(y + h // 2)

        def fill_rect(rx: int, ry: int, rw: int, rh: int, col_hex: str) -> None:
            r, g, b, a = _color_rgba(col_hex)
            cr.set_source_rgba(r, g, b, a * opacity)
            cr.rectangle(rx, ry, rw, rh)
            cr.fill()

        fill_rect(x0 + 4, y0, (x1 - x0) - 8, y1 - y0, border_col)
        fill_rect(x0, y0 + 4, x1 - x0, (y1 - y0) - 8, border_col)
        fill_rect(x0 + 4, y0 + 3, (x1 - x0) - 8, (y1 - y0) - 6, inner_border)
        fill_rect(x0 + 3, y0 + 4, (x1 - x0) - 6, (y1 - y0) - 8, inner_border)
        fill_rect(x0 + 6, y0 + 5, (x1 - x0) - 12, (y1 - y0) - 10, bg_col)

        # Downward-pointing pixel arrow toward the bee
        fill_rect(x - 8, y1, 16, 3, border_col)
        fill_rect(x - 5, y1 + 3, 10, 3, border_col)
        fill_rect(x - 2, y1 + 6, 4, 3, border_col)
        fill_rect(x - 6, y1 - 3, 12, 6, inner_border)
        fill_rect(x - 3, y1 + 3, 6, 3, inner_border)
        fill_rect(x - 4, y1 - 6, 8, 8, bg_col)
        fill_rect(x - 2, y1 + 2, 4, 2, bg_col)

        cr.set_antialias(cairo.ANTIALIAS_DEFAULT)
        tr, tg, tb, ta = _color_rgba(text_col)
        cr.set_source_rgba(tr, tg, tb, ta * opacity)
        tx = x - (ext.width / 2.0 + ext.x_bearing)
        ty = y - (ext.height / 2.0 + ext.y_bearing)
        cr.move_to(tx, ty)
        cr.show_text(disp_text)
        cr.set_antialias(cairo.ANTIALIAS_NONE)

    def render_to_cairo(self, cr) -> None:
        """Render the current widget frame onto any 32-bit ARGB Cairo context."""
        # Clear to 100% transparent alpha=0.0 background
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_antialias(cairo.ANTIALIAS_NONE)

        snap = self._frame_snapshot
        if snap is None:
            snap = self._compute_frame_state(time.time(), advance=False)
            self._frame_snapshot = snap

        now = snap["now"]
        eff_mode = snap["eff_mode"]
        sc = snap["sc"]
        cx = snap["cx"]
        bx = snap["bx"]
        by = snap["by"]
        shadow_w = snap["shadow_w"]
        shadow_y = snap["shadow_y"]
        mini_bee_positions = snap["mini_bee_positions"]
        mb_frame = snap["mb_frame"]
        p_scale = max(2, sc - 1)
        opacity = self.current_opacity
        use_group = opacity < 0.999
        layer_alpha = 1.0 if use_group else opacity
        if use_group:
            cr.push_group()

        # 1. Ground shadow (#2C1810)
        sr, sg, sb, sa = _color_rgba("#2C1810")
        cr.set_source_rgba(sr, sg, sb, sa * layer_alpha)
        cr.rectangle(cx - shadow_w, shadow_y, 2 * shadow_w, sc)
        cr.fill()

        # 2. Orbiting mini bees behind (Swarm mode)
        if eff_mode == "swarm":
            self._draw_mini_bees_cairo(cr, mini_bee_positions, mb_frame, p_scale, layer_alpha, behind=True)

        # 3. Main Pixel-Art Bee Sprite (44x40 base grid anchored at center)
        norm_key = snap.get("norm_key")
        if norm_key is None:
            norm_key = self.renderer._normalize_key(
                eff_mode,
                snap["wing_frame"],
                snap["eye_state"],
                snap["look_dx"],
                snap["look_dy"],
                snap["prop_frame"],
                snap["leg_shift"],
            )
        bee_cache_key = ("bee", sc, norm_key)
        bee_surf = self._surface_cache.get(bee_cache_key)
        if bee_surf is not None:
            self._surface_cache.move_to_end(bee_cache_key)
        else:
            bee_spans = self.renderer.get_bee_color_spans(*norm_key)
            bee_surf = self._get_or_render_surface(
                bee_cache_key,
                bee_spans,
                44 * sc,
                40 * sc,
                sc,
            )
        bee_x0 = bx - 22 * sc
        bee_y0 = by - 20 * sc
        cr.set_source_surface(bee_surf, bee_x0, bee_y0)
        if layer_alpha < 0.999:
            cr.paint_with_alpha(layer_alpha)
        else:
            cr.paint()

        # 4. Orbiting mini bees in front (Swarm mode)
        if eff_mode == "swarm":
            self._draw_mini_bees_cairo(cr, mini_bee_positions, mb_frame, p_scale, layer_alpha, behind=False)

        # 5. Pixel-Art Particles
        self._draw_particles_cairo(cr, p_scale, layer_alpha)

        # 6. Retro Pixel-Art RPG Dialogue Bubble (ONLY shown on mouse hover)
        if self.is_hovered:
            txt = self.bubble_text if now < self.bubble_until and self.bubble_text else (self.detail or "Ready")
            offset_top = (16 * sc) if eff_mode in ("needs_input", "error") else (12 * sc)
            bub_y = by - offset_top
            self._draw_pixel_speech_box_cairo(cr, bx, bub_y, txt, eff_mode, layer_alpha)

        if use_group:
            cr.pop_group_to_source()
            cr.paint_with_alpha(opacity)

    def on_draw(self, _widget, cr) -> bool:
        self.render_to_cairo(cr)
        return False

    def run(self) -> None:
        if not self._quitting and self.win is not None:
            Gtk.main()


def run_wayland_widget() -> None:
    """Launch the 32-bit ARGB Wayland/GTK3 bee widget main loop."""
    if not can_use_wayland_gtk():
        raise RuntimeError("GTK 3.0 and PyCairo are not available")
    app = KawaiiBeeWaylandWidget()
    app.run()


if __name__ == "__main__":
    run_wayland_widget()
