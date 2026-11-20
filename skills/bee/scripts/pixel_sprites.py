#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pixel-Art Sprite Engine for Antigravity Bee Companion.
Contains the color palette, 2D ASCII pixel matrices, cached tk.PhotoImage generator,
and backend-agnostic horizontal color-span generators for Tkinter & Wayland Cairo.
"""

from __future__ import annotations

from collections import OrderedDict

try:
    import tkinter as tk
except ImportError:
    tk = None  # Allows pure-Python / Wayland Cairo usage without python3-tk

try:
    from platform_adapter import TRANSPARENT_KEY
except ImportError:
    TRANSPARENT_KEY = "#010203"

PIXEL_SCALE = 4  # 44x40 base pixel grid -> 176x160 crisp screen pixels

PALETTE = {
    ".": TRANSPARENT_KEY,
    "O": "#2C1810",  # Deep warm espresso outline
    "H": "#FFF394",  # Creamy yellow top highlight
    "Y": "#FFCC33",  # Golden honey midtone
    "A": "#F59E0B",  # Warm amber lower shading
    "D": "#D97706",  # Deep honey belly rim
    "S": "#5C3422",  # Stripe top highlight
    "M": "#3E2013",  # Stripe chocolate midtone
    "W": "#FFFFFF",  # Pure white (eye shine, front wing, cloud)
    "C": "#BAE6FD",  # Soft sky cyan (wing shading)
    "B": "#60A5FA",  # Wing blue border/accent
    "P": "#FF758F",  # Cozy coral-pink blush cheeks
    "R": "#EF4444",  # Alert red / ruby gem
    "L": "#FCA5A5",  # Light red/pink highlight
    "G": "#22C55E",  # Success green
    "E": "#86EFAC",  # Light green highlight
    "K": "#0F172A",  # Terminal dark screen
    "T": "#38BDF8",  # Terminal cyan border / lens
    "N": "#94A3B8",  # Cloud/metal shadow gray
    "V": "#E2E8F0",  # Cloud light gray
    "U": "#F97316",  # Hard-hat / pencil orange
}


def hex_to_rgba(hex_str: str) -> tuple[float, float, float, float]:
    """Convert a '#RRGGBB' (or '#RRGGBBAA' / '.') color string to (r, g, b, a) floats in 0.0..1.0."""
    if not hex_str or hex_str == ".":
        return (0.0, 0.0, 0.0, 0.0)
    s = hex_str.lstrip("#")
    if len(s) == 6:
        return (
            int(s[0:2], 16) / 255.0,
            int(s[2:4], 16) / 255.0,
            int(s[4:6], 16) / 255.0,
            1.0,
        )
    if len(s) == 8:
        return (
            int(s[0:2], 16) / 255.0,
            int(s[2:4], 16) / 255.0,
            int(s[4:6], 16) / 255.0,
            int(s[6:8], 16) / 255.0,
        )
    raise ValueError(f"Invalid hex color: {hex_str!r}")


PALETTE_RGBA: dict[str, tuple[float, float, float, float]] = {
    k: ((0.0, 0.0, 0.0, 0.0) if k == "." else hex_to_rgba(v))
    for k, v in PALETTE.items()
}

PARTICLE_ASCII: dict[str, list[str]] = {
    "heart": [
        ".OO.OO.",
        "OPLOPRO",
        "OPRRRRO",
        ".ORRRO.",
        "..ORO..",
        "...O...",
    ],
    "sparkle": [
        "..H..",
        ".HWH.",
        "HWWWH",
        ".HWH.",
        "..H..",
    ],
    "zzz": [
        "OOOOO",
        "..COO",
        ".CO..",
        "COO..",
        "OOOOO",
    ],
    "mini_bee_0": [
        "...WW...",
        "..WCCW..",
        ".OHYMSO.",
        "OHYYMSMO",
        "OAYYMSMO",
        ".OOOOOO.",
    ],
    "mini_bee_1": [
        "........",
        "..WWWW..",
        ".OHYMSO.",
        "OHYYMSMO",
        "OAYYMSMO",
        ".OOOOOO.",
    ],
}


def grid_to_color_spans(grid: list[list[str]]) -> list[tuple[int, int, int, str]]:
    """
    Convert non-transparent pixels of a 2D sprite grid into unscaled
    horizontal color spans: (x_start, y, span_len, hex_color).
    Consecutive pixels sharing the same color on a row are merged.
    """
    spans: list[tuple[int, int, int, str]] = []
    for y, row in enumerate(grid):
        span_start: int | None = None
        span_col: str | None = None
        for x, ch in enumerate(row):
            if ch != ".":
                col = PALETTE.get(ch, ch)
                if span_start is None:
                    span_start = x
                    span_col = col
                elif col != span_col:
                    spans.append((span_start, y, x - span_start, span_col))
                    span_start = x
                    span_col = col
            else:
                if span_start is not None:
                    spans.append((span_start, y, x - span_start, span_col))
                    span_start = None
                    span_col = None
        if span_start is not None:
            spans.append((span_start, y, len(row) - span_start, span_col))
    return spans


_PARTICLE_SPANS: dict[str, tuple[list[tuple[int, int, int, str]], int, int]] = {
    kind: (
        grid_to_color_spans([list(r) for r in rows]),
        len(rows[0]) if rows else 0,
        len(rows),
    )
    for kind, rows in PARTICLE_ASCII.items()
}


def get_particle_color_spans(kind: str) -> tuple[list[tuple[int, int, int, str]], int, int]:
    """Return unscaled (spans, grid_w, grid_h) for a particle kind."""
    return _PARTICLE_SPANS.get(kind, ([], 0, 0))


class PixelArtRenderer:
    """Builds and caches crisp nearest-neighbor sprites (tk.PhotoImage and/or color spans)."""

    def __init__(self, scale: int = PIXEL_SCALE, use_tk: bool = True):
        self.scale = scale
        self.use_tk = bool(use_tk)
        self.cache: OrderedDict = OrderedDict()
        self.rects_cache: OrderedDict = OrderedDict()
        self.spans_cache: OrderedDict = OrderedDict()
        self.particle_cache: dict = {}
        self.particle_rects_cache: dict = {}
        self._init_particle_sprites()

    def set_scale(self, new_scale: int):
        if new_scale == self.scale:
            return
        self.scale = new_scale
        self.cache.clear()
        self.rects_cache.clear()
        self.particle_cache.clear()
        self.particle_rects_cache.clear()
        self._init_particle_sprites()

    @staticmethod
    def grid_to_color_spans(grid: list[list[str]]) -> list[tuple[int, int, int, str]]:
        """Delegate to module-level grid_to_color_spans()."""
        return grid_to_color_spans(grid)

    def get_particle_color_spans(self, kind: str) -> tuple[list[tuple[int, int, int, str]], int, int]:
        """Return unscaled (spans, grid_w, grid_h) for a particle kind."""
        return get_particle_color_spans(kind)

    @staticmethod
    def _evict_lru(cache_dict: OrderedDict, limit: int = 256, drop: int = 64):
        if len(cache_dict) >= limit:
            for _ in range(min(drop, len(cache_dict))):
                cache_dict.popitem(last=False)

    def grid_to_rects(self, grid: list[list[str]], scale: int | None = None) -> list[tuple[int, int, int, int]]:
        """
        Convert non-transparent pixels of a 2D sprite grid into a list of
        (dx, dy, width, height) rectangles relative to the sprite's top-left corner.
        Used for X11 XShape window transparency on Linux.
        """
        sc = scale or self.scale
        rects = []
        for y, row in enumerate(grid):
            x_start = None
            for x, ch in enumerate(row):
                if ch != ".":
                    if x_start is None:
                        x_start = x
                else:
                    if x_start is not None:
                        rects.append((x_start * sc, y * sc, (x - x_start) * sc, sc))
                        x_start = None
            if x_start is not None:
                rects.append((x_start * sc, y * sc, (len(row) - x_start) * sc, sc))
        return rects

    def grid_to_photo(self, grid: list[list[str]], scale: int | None = None) -> tk.PhotoImage:
        if tk is None:
            raise RuntimeError("tkinter is required for PhotoImage rendering")
        sc = scale or self.scale
        h = len(grid)
        w = len(grid[0]) if h > 0 else 0
        img = tk.PhotoImage(width=w, height=h)
        # Put only non-transparent horizontal spans so '.' remains true alpha=0 in PhotoImage.
        # This prevents overlapping sprites (mini-bees, particles) from punching holes on Windows
        # and avoids dark bounding boxes on Linux X11/Wayland.
        for y, row in enumerate(grid):
            x_start = None
            span_cols = []
            for x, ch in enumerate(row):
                if ch != ".":
                    if x_start is None:
                        x_start = x
                    span_cols.append(PALETTE.get(ch, ch))
                else:
                    if x_start is not None:
                        img.put("{" + " ".join(span_cols) + "}", to=(x_start, y))
                        x_start = None
                        span_cols = []
            if x_start is not None:
                img.put("{" + " ".join(span_cols) + "}", to=(x_start, y))
        return img.zoom(sc, sc)

    def _init_particle_sprites(self):
        p_scale = max(2, self.scale - 1)
        for kind, ascii_rows in PARTICLE_ASCII.items():
            grid = [list(r) for r in ascii_rows]
            if self.use_tk:
                self.particle_cache[kind] = self.grid_to_photo(grid, scale=p_scale)
            self.particle_rects_cache[kind] = (
                self.grid_to_rects(grid, scale=p_scale),
                (len(ascii_rows[0]) if ascii_rows else 0) * p_scale,
                len(ascii_rows) * p_scale,
            )

    @staticmethod
    def _normalize_key(
        eff_mode: str,
        wing_frame: int,
        eye_state: str,
        look_dx: int,
        look_dy: int,
        prop_frame: int,
        leg_shift: int,
    ) -> tuple:
        if eff_mode in ("idle", "sleep", "done", "swarm", "dragged", "petting", "error"):
            eff_prop = 0
        elif eff_mode == "coding":
            eff_prop = prop_frame % 4
        elif eff_mode == "web":
            eff_prop = prop_frame % 12
        elif eff_mode in ("thinking", "reading"):
            eff_prop = prop_frame % 3
        else:
            eff_prop = prop_frame % 2

        if eye_state in ("sleep", "happy", "dizzy"):
            eff_dx, eff_dy = 0, 0
        else:
            eff_dx = max(-1, min(1, look_dx))
            eff_dy = max(0, min(1, look_dy))

        if eff_mode == "sleep":
            eff_wing, eff_leg = 0, 0
        else:
            eff_wing, eff_leg = wing_frame, leg_shift

        return (eff_mode, eff_wing, eye_state, eff_dx, eff_dy, eff_prop, eff_leg)

    def build_bee_grid(
        self,
        eff_mode: str,
        wing_frame: int,
        eye_state: str,
        look_dx: int,
        look_dy: int,
        prop_frame: int,
        leg_shift: int,
    ) -> list[list[str]]:
        """Build the 44x40 2D ASCII pixel grid for a bee pose and mode."""
        eff_mode, wing_frame, eye_state, look_dx, look_dy, prop_frame, leg_shift = (
            self._normalize_key(
                eff_mode, wing_frame, eye_state, look_dx, look_dy, prop_frame, leg_shift
            )
        )

        W, H = 44, 40
        g = [["." for _ in range(W)] for _ in range(H)]

        def put_px(x: int, y: int, col: str):
            if 0 <= x < W and 0 <= y < H and col != ".":
                g[y][x] = col

        def draw_sprite(x0: int, y0: int, ascii_rows: list[str]):
            for dy, r in enumerate(ascii_rows):
                for dx, ch in enumerate(r):
                    if ch != ".":
                        put_px(x0 + dx, y0 + dy, ch)

        # 1. Fluffy cloud pillow in sleep mode
        if eff_mode == "sleep":
            cloud = [
                ".....OOOO...OOOOO...OOOO....",
                "...OOWWWWOOOWWWWWOOOWWWWOO..",
                "..OWWWWWWWWWWWWWWWWWWWWWWWO.",
                ".OWWWWWWWWWWWWWWWWWWWWWWWWWO",
                ".OWWWVVWWWWWWWWWWWWWWWVVWWWO",
                ".OOVVVVVVVVVVVVVVVVVVVVVVVOO",
                "..OONNNNNNNNNNNNNNNNNNNNNOO.",
                "....OOOOOOOOOOOOOOOOOOOOO...",
            ]
            draw_sprite(9, 25, cloud)

        # 2. Back Wing
        bx, by = 14, 14
        if eff_mode != "sleep":
            if wing_frame == 0:
                bw = [
                    ".OOOO.",
                    "OCCBCO",
                    "OCBBCO",
                    "OCCBCO",
                    ".OCCO.",
                ]
                draw_sprite(bx + 5, by - 5, bw)
            elif wing_frame == 1:
                bw = [
                    ".OOOOO.",
                    "OCCCBCO",
                    "OCBBBCO",
                    ".OCCCO.",
                ]
                draw_sprite(bx + 4, by - 4, bw)
            else:
                bw = [
                    ".OOOO.",
                    "OCCBCO",
                    ".OCCO.",
                ]
                draw_sprite(bx + 5, by - 3, bw)

        # 3. Chubby Cute Bee Body (22x13 rounded boxy silhouette)
        body = [
            "...OOOOOOOOOOOOOOOO...",
            "..OHHHHHHHSSHHSSHHHO..",
            ".OHHHHHHHHSSHHSSHHHHO.",
            "OHHHHHHHHHSSHHSSHHHHHO",
            "OYYYYYYYYYMMYYMMYYYYYO",
            "OYYYYYYYYYMMYYMMYYYYYO",
            "OYYYYYYYYYMMYYMMYYYYYO",
            "OYYYYYYYYYMMYYMMYYYYYO",
            "OYYYYYYYYYMMYYMMYYYYYO",
            "OAAAAAAAAAMMAAAMMAAAAO",
            ".OAAAAAAAAOOAAOOAAAAO.",
            "..ODDDDDDDOODDOODDDO..",
            "...OOOOOOOOOOOOOOOO...",
        ]
        draw_sprite(bx, by, body)

        # Stinger
        put_px(bx + 22, by + 6, "O")
        put_px(bx + 22, by + 7, "O")
        put_px(bx + 23, by + 6, "O")

        # 4. Tucked Pixel Paws
        if eff_mode != "sleep":
            for px in (bx + 5, bx + 11, bx + 16):
                sx = px + leg_shift
                put_px(sx, by + 13, "O")
                put_px(sx + 1, by + 13, "O")
                if eff_mode == "dragged":
                    put_px(sx, by + 14, "O")
                    put_px(sx + 1, by + 14, "O")

        # 5. Antennae
        ant_bob = 1 if wing_frame == 2 else 0
        put_px(bx + 3, by - 1 + ant_bob, "O")
        put_px(bx + 2, by - 2 + ant_bob, "O")
        put_px(bx + 1, by - 2 + ant_bob, "O")
        put_px(bx + 7, by - 1 + ant_bob, "O")
        put_px(bx + 6, by - 2 + ant_bob, "O")
        put_px(bx + 5, by - 2 + ant_bob, "O")

        # 6. Minimalist Adorable Face
        ex_shift = max(-1, min(1, look_dx))
        ey_shift = max(0, min(1, look_dy))
        lex = bx + 2 + ex_shift
        rex = bx + 7 + ex_shift
        ey = by + 5 + ey_shift

        # Rosy pixel cheeks
        put_px(lex - 1, by + 8, "P")
        put_px(lex, by + 8, "P")
        put_px(rex + 1, by + 8, "P")
        put_px(rex + 2, by + 8, "P")
        if eye_state in ("happy", "surprised"):
            put_px(lex - 1, by + 9, "P")
            put_px(rex + 2, by + 9, "P")

        if eye_state == "sleep":
            for x0 in (bx + 2, bx + 7):
                put_px(x0, by + 7, "O")
                put_px(x0 + 1, by + 7, "O")
        elif eye_state == "happy":
            for x0 in (bx + 2, bx + 7):
                put_px(x0, by + 6, "O")
                put_px(x0 + 1, by + 5, "O")
                put_px(x0 + 2, by + 6, "O")
        elif eye_state == "dizzy":
            put_px(bx + 2, by + 5, "O")
            put_px(bx + 3, by + 6, "O")
            put_px(bx + 2, by + 7, "O")
            put_px(bx + 8, by + 5, "O")
            put_px(bx + 7, by + 6, "O")
            put_px(bx + 8, by + 7, "O")
        elif eye_state == "surprised":
            for x0 in (lex, rex):
                for dy in range(3):
                    put_px(x0, ey + dy, "O")
                    put_px(x0 + 1, ey + dy, "O")
                put_px(x0, ey, "W")
                put_px(x0 + 1, ey + 2, "W")
        else:
            shine_dx = 1 if look_dx > 0 else 0
            for x0 in (lex, rex):
                for dy in range(3):
                    put_px(x0, ey + dy, "O")
                    put_px(x0 + 1, ey + dy, "O")
                put_px(x0 + shine_dx, ey, "W")

        # 7. Front Wing
        if eff_mode == "sleep":
            folded = [
                ".OOOOOOO.",
                "OWWWWWCCO",
                ".OOOOOOO.",
            ]
            draw_sprite(bx + 9, by - 1, folded)
        else:
            if wing_frame == 0:
                fw = [
                    "..OOOO..",
                    ".OWWWWO.",
                    "OWWWWWWO",
                    "OWWWCCWO",
                    "OWWCCCWO",
                    ".OWCCWO.",
                    "..OOOO..",
                ]
                draw_sprite(bx + 9, by - 6, fw)
            elif wing_frame == 1:
                fw = [
                    ".OOOOOO.",
                    "OWWWWWWO",
                    "OWWWCCWO",
                    "OWCCCCWO",
                    ".OOOOOO.",
                ]
                draw_sprite(bx + 9, by - 4, fw)
            else:
                fw = [
                    ".OOOOOO.",
                    "OWWWWWWO",
                    "OWWCCCWO",
                    ".OOOOOO.",
                ]
                draw_sprite(bx + 9, by - 2, fw)

        # 8. Mode Costumes & Pixel Accessories
        if eff_mode == "thinking":
            for x0 in (lex - 1, rex - 1):
                for gx in range(4):
                    put_px(x0 + gx, ey - 1, "U")
                    put_px(x0 + gx, ey + 3, "U")
                for gy in range(3):
                    put_px(x0, ey + gy, "U")
                    put_px(x0 + 3, ey + gy, "U")
            put_px(lex + 3, ey, "U")
            put_px(rex - 1, ey, "U")

            tb = [
                "..OOOOOOO...",
                ".OWWWWWWWWO.",
                "OWWWWWWWWWWO",
                "OWWWWWWWWWWO",
                ".OWWWWWWWWO.",
                "..OOOOOOO...",
                ".......OOO..",
                ".......OWO..",
                ".......OOO..",
                "..........OO",
            ]
            draw_sprite(2, 4, tb)
            for i in range((prop_frame % 3) + 1):
                put_px(5 + i * 2, 7, "O")

        elif eff_mode == "reading":
            scan_y = 14 + (prop_frame % 3)
            mag = [
                "..OOOO..",
                ".OWTTWO.",
                "OWTTTTWO",
                "OTTTTTTO",
                "OTTTTTTO",
                ".OTTTTO.",
                "..OOOO..",
                "....OO..",
                ".....OO.",
                "......OO",
            ]
            draw_sprite(2, scan_y, mag)

        elif eff_mode == "coding":
            py = 14 + (prop_frame % 2)
            px = 2 + ((prop_frame // 2) % 2)
            pencil = [
                "......OO",
                ".....OPO",
                "....OVVO",
                "...OUUUO",
                "..OUUUO.",
                ".OUUUO..",
                "OHHHO...",
                "OOHO....",
                "OOO.....",
            ]
            draw_sprite(px, py, pencil)

        elif eff_mode == "command":
            hat = [
                "...OOOOOO...",
                "..OUUUUUUO..",
                ".OUUUUUUUUO.",
                ".OUUHHHHUUO.",
                "OOOOOOOOOOOO",
            ]
            draw_sprite(bx + 1, by - 4, hat)

            term_y = 15 + (prop_frame % 2)
            term = [
                "OOOOOOOOOO",
                "OTTTTTTTTO",
                "OTKKKKKKTO",
                "OTKGKkkkTO",
                "OTKKGKKKTO",
                "OTKGKKKKTO",
                "OTKKKKKKTO",
                "OTTTTTTTTO",
                "OOOOOOOOOO",
            ]
            cur_col = "G" if (prop_frame % 2 == 0) else "K"
            term_fixed = [r.replace("k", cur_col) for r in term]
            draw_sprite(1, term_y, term_fixed)

        elif eff_mode == "web":
            goggles = [
                ".OOOO..OOOO.",
                "OTWWTOOTWWTO",
                "OTTTTOOTTTTO",
                ".OOOO..OOOO.",
            ]
            draw_sprite(bx + 1, by + 1, goggles)
            wx = (prop_frame * 3) % 10
            for wy in (10, 20, 28):
                put_px(2 + wx, wy, "W")
                put_px(3 + wx, wy, "C")
                put_px(4 + wx, wy, "C")

        elif eff_mode == "swarm":
            crown = [
                ".O...O...O.",
                "OHO.OHO.OHO",
                "OYYOYYYOYHO",
                "OYYYYRYYYYO",
                "OOOOOOOOOOO",
            ]
            draw_sprite(bx + 2, by - 4, crown)

        elif eff_mode == "needs_input":
            ay = 1 + (prop_frame % 2)
            alert = [
                "..OOOOOOO..",
                ".ORLLLLLRO.",
                "ORLRRRRRRRO",
                "ORLRRWRRRRO",
                "ORLRRWRRRRO",
                "ORLRRWRRRRO",
                "ORLRRRRRRRO",
                "ORLRRWRRRRO",
                ".ORRRRRRRO.",
                "..OOOOOOO..",
            ]
            draw_sprite(bx + 5, ay, alert)

        elif eff_mode == "done":
            # Calm green pixel checkmark button [✓] ONLY (no hyperactive props!)
            check = [
                "..OOOOOOO..",
                ".OGEEEEEGO.",
                "OGGGGGGGGGO",
                "OGGGGGGGWGO",
                "OGGGGGGWGGO",
                "OGWGGGWGGGO",
                "OGGWGWGGGGO",
                "OGGGWGGGGGO",
                ".OGGGGGGGGO",
                "..OOOOOOO..",
            ]
            draw_sprite(2, 12, check)

        elif eff_mode == "eating":
            # Honey pot only when explicitly fed honey!
            hy = 17 + (prop_frame % 2)
            honeypot = [
                "..OOOOOOOO..",
                ".OHHHHHHHHO.",
                ".OYYYYYYYYO.",
                "..OHYHHYHO..",
                ".ODDDDDDDDO.",
                "ODDDDDDDDDDO",
                "ODDYYYYYYDDO",
                "ODDDDDDDDDDO",
                ".ODDDDDDDDO.",
                "..OOOOOOOO..",
            ]
            draw_sprite(1, hy, honeypot)

        elif eff_mode == "error":
            warn = [
                "....OOO....",
                "...OYYYO...",
                "..OYYYYYO..",
                "..OYYOYYO..",
                ".OYYYOYYYO.",
                ".OYYYOYYYO.",
                "OYYYYYYYYYO",
                "OYYYYOYYYYO",
                "OYYYYYYYYYO",
                ".OOOOOOOOO.",
            ]
            draw_sprite(bx + 6, 1, warn)

        return g

    def get_bee_color_spans(
        self,
        eff_mode: str,
        wing_frame: int,
        eye_state: str,
        look_dx: int,
        look_dy: int,
        prop_frame: int,
        leg_shift: int,
    ) -> list[tuple[int, int, int, str]]:
        """Return cached unscaled (x_start, y, span_len, hex_color) spans for the 44x40 bee."""
        key = self._normalize_key(
            eff_mode, wing_frame, eye_state, look_dx, look_dy, prop_frame, leg_shift
        )
        if key not in self.spans_cache:
            g = self.build_bee_grid(*key)
            self._evict_lru(self.spans_cache)
            self.spans_cache[key] = self.grid_to_color_spans(g)
        else:
            self.spans_cache.move_to_end(key)
        return self.spans_cache[key]

    def get_bee_sprite(
        self,
        eff_mode: str,
        wing_frame: int,
        eye_state: str,
        look_dx: int,
        look_dy: int,
        prop_frame: int,
        leg_shift: int,
    ) -> tk.PhotoImage:
        if not self.use_tk:
            raise RuntimeError("get_bee_sprite() requires use_tk=True")
        key = self._normalize_key(
            eff_mode, wing_frame, eye_state, look_dx, look_dy, prop_frame, leg_shift
        )
        if key in self.cache:
            self.cache.move_to_end(key)
            if key in self.rects_cache:
                self.rects_cache.move_to_end(key)
            if key in self.spans_cache:
                self.spans_cache.move_to_end(key)
            return self.cache[key]

        g = self.build_bee_grid(*key)
        photo = self.grid_to_photo(g)
        rects = self.grid_to_rects(g)
        self._evict_lru(self.cache)
        self._evict_lru(self.rects_cache)
        self.cache[key] = photo
        self.rects_cache[key] = rects
        if key not in self.spans_cache:
            self._evict_lru(self.spans_cache)
            self.spans_cache[key] = self.grid_to_color_spans(g)
        return photo

    def get_bee_rects(
        self,
        eff_mode: str,
        wing_frame: int,
        eye_state: str,
        look_dx: int,
        look_dy: int,
        prop_frame: int,
        leg_shift: int,
    ) -> list[tuple[int, int, int, int]]:
        key = self._normalize_key(
            eff_mode, wing_frame, eye_state, look_dx, look_dy, prop_frame, leg_shift
        )
        if key not in self.rects_cache:
            if self.use_tk:
                self.get_bee_sprite(*key)
            else:
                g = self.build_bee_grid(*key)
                self._evict_lru(self.rects_cache)
                self.rects_cache[key] = self.grid_to_rects(g)
                if key not in self.spans_cache:
                    self._evict_lru(self.spans_cache)
                    self.spans_cache[key] = self.grid_to_color_spans(g)
        else:
            self.rects_cache.move_to_end(key)
        return self.rects_cache.get(key, [])
