"""Screen geometry.

Every position is given in *reference pixels* of a 1920x1080 (16:9) game image and is
scaled to the real stream. Item-card sub-regions are relative to the card itself
(reference card = 494x840). Values come from Inventory_Kamera's 16:9 layout and can be
overridden in config.json (same structure, only the keys you want to change).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .winutil import Rect

REF_W, REF_H = 1920, 1080

DEFAULT = {
    "window_title": "GeForce NOW",  # regex matched against window titles
    "tesseract_cmd": None,
    # Names shown in game -> official character, for renamed characters:
    # {"MyTravelerName": "Traveler", "Kunikuzushi": "Wanderer"}
    "name_aliases": {},
    # Part of the window that contains the 16:9 game image: "auto" or [x, y, w, h] in
    # fractions of the window client area (for black bars or unusual aspect ratios).
    "frame": "auto",
    "inventory": {
        "card": [1306, 119, 494, 840],
        # Scrolling viewport of the tiles (left part); the artifacts tab has the
        # "Show 5-star Artifacts in the order..." toggle above it.
        "grid_area": {"artifacts": [100, 165, 1200, 795], "weapons": [100, 110, 1200, 850]},
        "columns": 8,
        "tile_size": [123, 152],
        "first_tile_center": [178.5, 253],  # artifacts tab, scrolled to the top
        "tile_pitch": [146.4, 175.3],
        "notches_per_row": 10,  # first guess only, measured during the scan
        # Scrollbar of the grid: light thumb in a dark track (x, then y range of the track).
        "scrollbar_x": 1290,
        "scrollbar_track": {"artifacts": [165, 960], "weapons": [110, 960]},
        "item_count": [1545, 30, 263, 38],  # "1234/2400" box, top right
    },
    "card": {  # relative to the card, reference size 494x840
        "size": [494, 840],
        "name_plate": [0, 0, 494, 58],
        "slot": [20, 65, 260, 40],
        "main_name": [20, 145, 250, 36],  # stops before the artifact picture
        "main_value": [20, 180, 250, 58],
        "level": [25, 305, 72, 36],
        "lock": [370, 297, 47, 46],
        "substats": [30, 350, 440, 205],
        "body": [20, 345, 455, 430],  # level badge -> set bonus text
        "equipped": [74, 786, 420, 54],
        "sanctify_probe": [6, 286, 10, 10],
        "sanctify_color": [220, 192, 255],
        "sanctify_shift": 44,
        "weapon_level": [28, 306, 140, 36],  # "Lv. 90/90" dark badge
        "weapon_refinement": [72, 350, 260, 36],  # "Refinement Rank 2" (gold text)
        "weapon_lock": [424, 298, 50, 50],
    },
    "rarity_colors": {  # name plate gradient, dark and light shades
        "5": [[145, 81, 40], [188, 105, 50]], "4": [[121, 67, 167], [161, 86, 224]],
        "3": [[62, 96, 149], [81, 127, 203]], "2": [[42, 143, 114]], "1": [[114, 119, 138]],
    },
    # [colour, minimum share of the lock region] -> locked
    "lock_colors": [[[70, 80, 100], 0.25], [[255, 137, 117], 0.04]],
    # Light label under each inventory tile ("+20", "Lv. 90"); used to find rows and empty cells.
    "tile_label": {"min": 200, "max_spread": 40, "height": 25},
    "character": {
        "name": [128, 15, 400, 68],  # "Geo / Zibai" (long names wrap on two lines)
        "name_panel": [1460, 128, 340, 44],  # name above "Level 90 / 90", Attributes tab
        "level": [1440, 203, 260, 42],  # "Level 90 / 90"
        "tab_attributes": [230, 153],
        "tab_constellation": [250, 362],
        "tab_talents": [215, 432],
        "next": [1848, 540],
        # Node centres in the Constellation tab. An activated node has a bright gold ring
        # (radius ~32-38 px), a locked one a dark ring around its padlock.
        "constellation_nodes": [[1521, 276], [1573, 386], [1610, 498],
                                [1610, 610], [1573, 722], [1502, 832]],
        "constellation_ring": [32, 38],
        "constellation_ring_min_lum": 120,
        # Right column of the Talents tab: talent names with "Lv. N" under the combat ones.
        "talent_list": [1330, 130, 375, 620],
    },
    "timing": {
        "click_settle": 0.12,  # minimum wait after clicking an item
        "panel_timeout": 1.5,
        "ui": 1.2,  # opening menus / switching tabs
        "next_character": 0.8,
        "constellation_wait": 2.0,  # camera animation of the Constellation tab
        "talents_wait": 1.5,
        "scroll_settle": 0.35,
    },
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path: Path | None) -> dict:
    cfg = copy.deepcopy(DEFAULT)
    if path and path.exists():
        _merge(cfg, json.loads(path.read_text(encoding="utf-8")))
    return cfg


class Frame:
    """Maps reference coordinates to screen coordinates for the current stream."""

    def __init__(self, client: Rect, frame_cfg="auto"):
        if frame_cfg != "auto":
            fx, fy, fw, fh = frame_cfg
            self.rect = Rect(client.x + round(fx * client.w), client.y + round(fy * client.h),
                             round(fw * client.w), round(fh * client.h))
        else:
            # Wider than 16:9 -> pillarboxed; taller (16:10) -> assume letterboxed until calibrated.
            w, h = client.w, client.h
            if w * 9 > h * 16:
                fw = round(h * 16 / 9)
                self.rect = Rect(client.x + (w - fw) // 2, client.y, fw, h)
            else:
                fh = round(w * 9 / 16)
                self.rect = Rect(client.x, client.y + (h - fh) // 2, w, fh)
        self.sx = self.rect.w / REF_W
        self.sy = self.rect.h / REF_H

    def pt(self, x: float, y: float) -> tuple[int, int]:
        return self.rect.x + round(x * self.sx), self.rect.y + round(y * self.sy)

    def box(self, r) -> Rect:
        x, y, w, h = r
        return Rect(self.rect.x + round(x * self.sx), self.rect.y + round(y * self.sy),
                    max(1, round(w * self.sx)), max(1, round(h * self.sy)))

    def local(self, r) -> tuple[int, int, int, int]:
        """Reference rect -> crop box inside a capture of the frame."""
        x, y, w, h = r
        return (round(x * self.sx), round(y * self.sy), round((x + w) * self.sx), round((y + h) * self.sy))
