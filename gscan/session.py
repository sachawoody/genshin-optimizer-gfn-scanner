"""Link between the GeForce NOW window, the layout and the input helpers."""
from __future__ import annotations

import time
from pathlib import Path

from PIL import Image

from . import winutil
from .layout import Frame
from .winutil import Rect


class Session:
    def __init__(self, cfg: dict, window_pattern: str, debug_dir: Path, save_images: bool = False):
        winutil.set_dpi_aware()
        self.cfg = cfg
        self.t = cfg["timing"]
        self.debug_dir = debug_dir
        self.save_images = save_images
        hit = winutil.find_window(window_pattern)
        if hit:
            self.hwnd, self.title = hit
            client = winutil.client_rect(self.hwnd)
        else:
            print(f"[!] No window matches {window_pattern!r}: using the primary screen")
            self.hwnd, self.title = None, "primary screen"
            client = winutil.primary_screen_rect()
        self.client = client
        self.frame = Frame(client, cfg["frame"])
        self.mouse = winutil.Mouse()

    def describe(self) -> str:
        c, f = self.client, self.frame.rect
        return (f"window: {self.title!r}\n  client area: {c.w}x{c.h} @ ({c.x},{c.y})\n"
                f"  game image (16:9): {f.w}x{f.h} @ ({f.x},{f.y})  scale {self.frame.sx:.3f}")

    def focus(self) -> None:
        if self.hwnd:
            winutil.focus_window(self.hwnd)

    def grab(self, ref_rect=None) -> Image.Image:
        return winutil.grab(self.frame.box(ref_rect) if ref_rect else self.frame.rect)

    def box(self, ref_rect) -> Rect:
        return self.frame.box(ref_rect)

    def click(self, x: float, y: float) -> None:
        self.mouse.click(*self.frame.pt(x, y))

    def key(self, vk: int, wait: float | None = None) -> None:
        self.mouse.check_abort()
        winutil.key_tap(vk)
        time.sleep(self.t["ui"] if wait is None else wait)

    def save_debug(self, img: Image.Image, name: str) -> Path:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        p = self.debug_dir / name
        img.save(p)
        return p
