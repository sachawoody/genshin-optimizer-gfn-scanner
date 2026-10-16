"""Windows helpers: DPI awareness, window lookup, screen capture, synthetic input.

Only ctypes + Pillow, so nothing extra has to be installed. Input goes through
SendInput (scancodes / absolute mouse), which the GeForce NOW client forwards to
the remote game like real hardware input.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import re
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageGrab

user32 = ctypes.WinDLL("user32", use_last_error=True)


def set_dpi_aware() -> None:
    """Work in physical pixels, otherwise capture/click coordinates are scaled by Windows."""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()


# --------------------------------------------------------------------------- windows

@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.w, self.y + self.h)


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def list_windows() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def find_window(pattern: str) -> tuple[int, str] | None:
    """Return the visible window whose title matches `pattern`, preferring titles that mention Genshin."""
    rx = re.compile(pattern, re.I)
    hits = [(h, t) for h, t in list_windows() if rx.search(t)]
    hits.sort(key=lambda ht: ("genshin" not in ht[1].lower(), len(ht[1])))
    return hits[0] if hits else None


def client_rect(hwnd: int) -> Rect:
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return Rect(pt.x, pt.y, r.right - r.left, r.bottom - r.top)


def primary_screen_rect() -> Rect:
    return Rect(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


def focus_window(hwnd: int) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # A plain SetForegroundWindow is refused when another app has focus; an Alt tap unlocks it.
    key_tap(0x12, hold=0.02)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)


# --------------------------------------------------------------------------- capture

def grab(rect: Rect) -> Image.Image:
    return ImageGrab.grab(bbox=rect.box, all_screens=True).convert("RGB")


def frame_diff(a: Image.Image, b: Image.Image) -> float:
    """Mean absolute difference on a small grayscale thumbnail (robust to stream noise)."""
    size = (max(1, a.width // 4), max(1, a.height // 4))
    x = np.asarray(a.convert("L").resize(size), dtype=np.int16)
    y = np.asarray(b.convert("L").resize(size), dtype=np.int16)
    return float(np.abs(x - y).mean())


def changed(a: Image.Image, b: Image.Image, block: int = 16, min_share: float = 0.05) -> bool:
    """True when the content differs, not just stream noise.

    Two item cards can differ by only a few characters ("Lv. 70" vs "Lv. 80"), far too
    little for a global mean; so look for any small block whose pixels changed strongly.
    """
    x = np.asarray(a.convert("L"), dtype=np.int16)
    y = np.asarray(b.convert("L").resize(a.size), dtype=np.int16)
    d = np.abs(x - y) > 40
    h, w = (d.shape[0] // block) * block, (d.shape[1] // block) * block
    if not h or not w:
        return bool(d.mean() > min_share)
    blocks = d[:h, :w].reshape(h // block, block, w // block, block).mean(axis=(1, 3))
    return bool(blocks.max() > min_share)


def wait_stable(rect: Rect, timeout: float = 2.0, interval: float = 0.06,
                threshold: float = 1.2, prev: Image.Image | None = None,
                must_change: bool = False, still_frames: int = 1) -> Image.Image:
    """Grab `rect` until `still_frames` consecutive comparisons match (the stream settled).

    With `must_change`, first wait (within the same timeout) for the image to differ
    from `prev`: through the stream, an action shows up only after some latency, and the
    unchanged image before it would otherwise look perfectly "stable".
    """
    end = time.monotonic() + timeout
    img = grab(rect)
    if must_change and prev is not None:
        while not changed(img, prev) and time.monotonic() < end:
            time.sleep(interval)
            img = grab(rect)
    still = 0
    while time.monotonic() < end:
        time.sleep(interval)
        nxt = grab(rect)
        still = still + 1 if frame_diff(img, nxt) < threshold else 0
        img = nxt
        if still >= still_frames:
            return img
    return img


# --------------------------------------------------------------------------- input

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x1, 0x2, 0x4
MOUSEEVENTF_WHEEL, MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x800, 0x4000, 0x8000
KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x2, 0x8


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    return INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)))


class Aborted(Exception):
    pass


class Mouse:
    """Absolute mouse moves/clicks on the virtual desktop, with a 'user moved the mouse' abort."""

    def __init__(self, abort_distance: int = 60):
        self.abort_distance = abort_distance
        self.last: tuple[int, int] | None = None

    @staticmethod
    def pos() -> tuple[int, int]:
        p = wt.POINT()
        user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    def check_abort(self) -> None:
        if user32.GetAsyncKeyState(0x23) & 0x8000:  # End key
            raise Aborted("End key")
        if self.last is not None and self.abort_distance:
            x, y = self.pos()
            if abs(x - self.last[0]) + abs(y - self.last[1]) > self.abort_distance:
                raise Aborted("mouse moved")

    def move(self, x: int, y: int) -> None:
        self.check_abort()
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        nx = round((x - vx) * 65535 / max(1, vw - 1))
        ny = round((y - vy) * 65535 / max(1, vh - 1))
        _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))
        time.sleep(0.03)
        self.last = self.pos()

    def click(self, x: int, y: int, hold: float = 0.05) -> None:
        self.move(x, y)
        _send(_mouse(MOUSEEVENTF_LEFTDOWN))
        time.sleep(hold)
        _send(_mouse(MOUSEEVENTF_LEFTUP))

    def wheel(self, notches: int, pause: float = 0.03) -> None:
        """notches > 0 scrolls down."""
        self.check_abort()
        for _ in range(abs(notches)):
            _send(_mouse(MOUSEEVENTF_WHEEL, data=-120 if notches > 0 else 120))
            time.sleep(pause)


def key_tap(vk: int, hold: float = 0.05) -> None:
    sc = user32.MapVirtualKeyW(vk, 0)
    down = INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(0, sc, KEYEVENTF_SCANCODE, 0, 0)))
    up = INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(0, sc, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0, 0)))
    _send(down)
    time.sleep(hold)
    _send(up)
