"""Walk the inventory grid (artifacts or weapons tab): click every tile, capture its card.

Rows are found on screen from the light label under each tile, and the scroll position is
tracked by measuring how far the grid image actually moved after each wheel burst, so the
scan does not depend on an exact "notches per row" value (stream latency, inertia...).
"""
from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
from PIL import Image

from . import winutil
from .session import Session


@dataclass
class Row:
    top: float  # tile top, reference px, on screen
    cols: list[int]  # occupied columns


class Grid:
    def __init__(self, s: Session, tab: str = "artifacts"):
        self.s = s
        inv = s.cfg["inventory"]
        self.inv = inv
        self.area = inv["grid_area"][tab]
        self.track = inv["scrollbar_track"][tab]
        self.tile_w, self.tile_h = inv["tile_size"]
        self.pitch_x, self.pitch_y = inv["tile_pitch"]
        self.x0 = inv["first_tile_center"][0]
        self.cols = inv["columns"]
        self.px_per_notch = self.pitch_y / inv["notches_per_row"]  # reference px

    def col_x(self, i: int) -> float:
        return self.x0 + i * self.pitch_x

    def detect_rows(self, img: Image.Image) -> list[Row]:
        """Fully visible rows in a frame capture, from the light tile labels."""
        f, lbl = self.s.frame, self.s.cfg["tile_label"]
        gx, gy, gw, gh = self.area
        a = np.asarray(img, dtype=np.int16)
        y0, y1 = round(gy * f.sy), round((gy + gh) * f.sy)
        per_col = []
        for i in range(self.cols):
            cx = self.col_x(i)
            x0, x1 = round((cx - self.tile_w * 0.3) * f.sx), round((cx + self.tile_w * 0.3) * f.sx)
            strip = a[y0:y1, x0:x1]
            cream = (strip.min(axis=2) >= lbl["min"]) & (np.ptp(strip, axis=2) <= lbl["max_spread"])
            per_col.append(cream.mean(axis=1) > 0.6)
        per_col = np.array(per_col)  # cols x rows(px)
        any_col = per_col.any(axis=0)
        lo_run, hi_run = lbl["height"] * 0.5 * f.sy, lbl["height"] * 1.6 * f.sy
        bands = []  # (bottom_ref, occupied cols, cut by the grid edge)
        y = 0
        n = len(any_col)
        while y < n:
            if not any_col[y]:
                y += 1
                continue
            start = y
            while y < n and any_col[y]:
                y += 1
            if not lo_run <= y - start <= hi_run:
                continue  # not label-shaped (light icon, highlight...)
            occupied = [i for i in range(self.cols) if per_col[i, start:y].mean() > 0.5]
            bottom_ref = gy + y / f.sy
            # Skip tiles cut by the viewport: clicking a hidden centre could hit the controls around it.
            cut = bottom_ref - self.tile_h / 2 < gy + 10 or y >= n - 1
            bands.append((bottom_ref, occupied, cut))
        if not bands:
            return []
        # Real rows sit on a regular pitch: keep the bands in phase with the fullest one.
        ref = max(bands, key=lambda b: len(b[1]))[0]
        tol = self.pitch_y * 0.12
        rows = []
        for bottom, occupied, cut in bands:
            phase = (bottom - ref) % self.pitch_y
            if min(phase, self.pitch_y - phase) <= tol and not cut and occupied:
                rows.append(Row(bottom - self.tile_h, occupied))
        return rows

    def fallback_rows(self) -> list[Row]:
        gy, gh = self.area[1], self.area[3]
        top = self.inv["first_tile_center"][1] - self.tile_h / 2
        out = []
        while top + self.tile_h <= gy + gh:
            out.append(Row(top, list(range(self.cols))))
            top += self.pitch_y
        return out

    def thumb(self, img: Image.Image) -> float | None:
        """Centre of the scrollbar thumb (reference px), or None if not found."""
        f = self.s.frame
        x = self.inv["scrollbar_x"]
        y0, y1 = self.track
        a = np.asarray(img.convert("L").crop(f.local((x - 3, y0, 6, y1 - y0))), dtype=np.float32).mean(axis=1)
        bright = a > 150
        best, start = (0, 0), None
        for i, b in enumerate(np.append(bright, False)):
            if b and start is None:
                start = i
            elif not b and start is not None:
                if i - start > best[1] - best[0]:
                    best = (start, i)
                start = None
        if (best[1] - best[0]) / f.sy < 15:
            return None
        return y0 + (best[0] + best[1]) / 2 / f.sy

    def grid_gray(self, img: Image.Image) -> np.ndarray:
        x, y, x2, y2 = self.s.frame.local(self.area)
        g = img.crop((x, y, x2, y2)).convert("L")
        return np.asarray(g.resize((max(1, g.width // 2), max(1, g.height // 2))), dtype=np.int16)

    def measure_shift(self, before: np.ndarray, after: np.ndarray, expected: float) -> float:
        """Signed vertical scroll in reference px (> 0: content moved up, i.e. scrolled down).
        Exact within a row, but rows of identical items can fool it by whole rows: the
        scrollbar thumb settles that (InventoryScanner.scroll)."""
        if expected < 0:
            return -self.measure_shift(after, before, -expected)
        h = before.shape[0]
        sy = self.s.frame.sy / 2  # grid_gray is half resolution
        exp = expected * sy
        hi = int(min(h * 0.85, exp * 1.5 + self.pitch_y * sy))
        best, best_err = 0, float("inf")
        for sft in range(0, hi + 1):
            err = float(np.abs(before[sft:] - after[:h - sft]).mean())
            # Slight bias toward the expected value against near-periodic grids.
            err += 0.02 * abs(sft - exp) / max(1.0, exp)
            if err < best_err:
                best, best_err = sft, err
        return best / sy


class InventoryScanner:
    """Generic loop; `on_card(img, index, same_as_previous)` returns False to stop the scan."""

    def __init__(self, s: Session, tab: str, workers: int = 3):
        self.s = s
        self.grid = Grid(s, tab)
        self.pool = ThreadPoolExecutor(workers)
        self.ratio: float | None = None  # content px per scrollbar-thumb px

    def run(self, on_card, max_items: int = 3000) -> int:
        s, g = self.s, self.grid
        card_ref = s.cfg["inventory"]["card"]
        card_rect = s.box(card_ref)
        img = s.grab()
        rows = g.detect_rows(img)
        if not rows:
            print("[!] Inventory rows not detected: using a fixed grid (check the calibration)")
        offset = 0.0  # content scrolled so far, reference px
        last_top = -1e9  # content position of the last scanned row
        prev_card = None  # the first item is usually already selected: nothing to compare
        count = 0
        calibrated = False
        page = 0
        while True:
            rows = g.detect_rows(img) or g.fallback_rows()
            new_rows = [r for r in rows if r.top + offset > last_top + g.pitch_y / 2]
            page += 1
            self.save_page(img, rows, new_rows, page)
            self.log(f"page {page}: total shift {offset:.0f}, rows {[round(r.top) for r in rows]}, "
                     f"new {[round(r.top) for r in new_rows]}, items read {count}")
            for row in new_rows:
                for col in range(g.cols):
                    if row is rows[-1] and not any(c in row.cols for c in range(col, g.cols)):
                        return count  # last row filled only up to here -> end of the inventory
                    card = self.select(g.col_x(col), row.top + g.tile_h / 2, card_rect, prev_card)
                    if prev_card is not None and not winutil.changed(card, prev_card):
                        # Same card as before: the click may have been lost in the stream.
                        card = self.select(g.col_x(col), row.top + g.tile_h / 2, card_rect, prev_card)
                    same = prev_card is not None and not winutil.changed(card, prev_card)
                    prev_card = card
                    if on_card(card, count, same) is False:
                        return count + 1
                    count += 1
                    if count >= max_items:
                        return count
                last_top = row.top + offset

            if not calibrated:
                # A few notches first, to measure the real scroll speed through the stream.
                img, shift = self.scroll(img, 3)
                if shift < 2:
                    return count  # nothing moved: the whole inventory fits on one page
                g.px_per_notch = shift / 3
                calibrated = True
                offset += shift
                continue

            # Bring the first unscanned row to the second row slot: one row of overlap keeps
            # enough common image between the two captures to measure the real shift.
            slot = rows[1].top if len(rows) > 1 else rows[0].top
            target = (last_top - offset + g.pitch_y) - slot
            notches = max(1, int(target / g.px_per_notch) - 1)
            img, shift = self.scroll(img, notches)
            if shift < 2:
                img, shift = self.scroll(img, notches)  # second try, in case input was dropped
                if shift < 2:
                    return count  # bottom reached: everything left was visible and scanned
            if shift > 0.6 * notches * g.px_per_notch:  # partial scrolls happen at the bottom
                g.px_per_notch = 0.5 * g.px_per_notch + 0.5 * shift / notches
            offset += shift

            # Overshoot guard: if a row may have been skipped, scroll back up.
            rows = g.detect_rows(img)
            if rows and rows[0].top + offset > last_top + g.pitch_y * 1.5:
                back = -(int((rows[0].top + offset - last_top) / g.px_per_notch) + 1)
                img, shift = self.scroll(img, back)
                offset += shift

    def select(self, x: float, y: float, card_rect, prev_card: Image.Image | None) -> Image.Image:
        self.s.click(x, y)
        time.sleep(self.s.t["click_settle"])
        return winutil.wait_stable(card_rect, timeout=self.s.t["panel_timeout"],
                                   prev=prev_card, must_change=prev_card is not None)

    def scroll(self, img: Image.Image, notches: int) -> tuple[Image.Image, float]:
        """Wheel, wait for the grid to start moving (stream latency) then to stop, and measure
        the shift: image registration for the exact position within a row, the scrollbar
        thumb for the number of rows (identical rows of duplicates fool the registration)."""
        s, g = self.s, self.grid
        before = g.grid_gray(img)
        thumb_before = g.thumb(img)
        grid_rect = s.box(g.area)  # includes the scrollbar, so it "changes" even then
        before_img = winutil.grab(grid_rect)
        gx, gy, gw, gh = g.area
        s.mouse.move(*s.frame.pt(g.col_x(g.cols // 2), gy + gh / 2))
        s.mouse.wheel(notches)
        time.sleep(s.t["scroll_settle"])
        winutil.wait_stable(grid_rect, timeout=3.0, interval=0.1, prev=before_img, must_change=True,
                            still_frames=2)
        img = s.grab()
        expected = notches * g.px_per_notch
        shift = g.measure_shift(before, g.grid_gray(img), expected)
        thumb_after = g.thumb(img)
        note = ""
        if thumb_before is not None and thumb_after is not None:
            moved = thumb_after - thumb_before
            if self.ratio:
                rows_off = round((moved * self.ratio - shift) / g.pitch_y)
                if rows_off:
                    note = f" ; scrollbar: {rows_off:+d} row(s) -> corrected"
                    shift += rows_off * g.pitch_y
                elif abs(moved) >= 10:
                    self.ratio = 0.8 * self.ratio + 0.2 * shift / moved
            elif abs(moved) >= 10 and abs(shift - expected) < 0.3 * g.pitch_y:
                # First clear move (top of the list, distinct items): learn content px / thumb px.
                self.ratio = shift / moved
            note += f" ; scrollbar {thumb_before:.0f}->{thumb_after:.0f}"
        self.log(f"wheel {notches:+d} notches -> shift {shift:.0f} px (expected {expected:.0f}){note}")
        return img, shift

    def log(self, msg: str) -> None:
        if self.s.save_images:
            self.s.debug_dir.mkdir(parents=True, exist_ok=True)
            with open(self.s.debug_dir / "grid.log", "a", encoding="utf-8") as f:
                f.write(msg + "\n")

    def save_page(self, img: Image.Image, rows: list[Row], new_rows: list[Row], page: int) -> None:
        """Debug: the grid as seen, with the detected rows and the click points."""
        if not self.s.save_images:
            return
        from PIL import ImageDraw
        f, g = self.s.frame, self.grid
        out = img.copy()
        d = ImageDraw.Draw(out)
        for r in rows:
            for c in range(g.cols):
                x0, y0, x1, y1 = f.local((g.col_x(c) - g.tile_w / 2, r.top, g.tile_w, g.tile_h))
                d.rectangle((x0, y0, x1, y1), outline="lime" if r in new_rows else "gray", width=3)
                if r in new_rows:
                    cx, cy = f.local((g.col_x(c), r.top + g.tile_h / 2, 0, 0))[:2]
                    d.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill="red")
        self.s.debug_dir.mkdir(parents=True, exist_ok=True)
        out.resize((out.width // 2, out.height // 2)).save(self.s.debug_dir / f"page_{page:03d}.png")

    def submit(self, fn, *args) -> Future:
        return self.pool.submit(fn, *args)
