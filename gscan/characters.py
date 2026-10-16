"""Character screen: name, level/ascension, constellation and talents of 5* characters.

For each character: Attributes tab (name + level), Constellation tab (one capture: activated
nodes have a bright gold ring), Talents tab (the list shows "Lv. N" under the combat
talents), then the right arrow to the next character until the first one comes back.
"""
from __future__ import annotations

import re
import time

import numpy as np
from PIL import Image
from rapidfuzz import fuzz, process

from . import ocr
from .gamedata import ELEMENTS, GameData
from .session import Session
from .stats import ascension_from_cap, parse_level_cap

ELEMENT_NAMES = sorted(set(ELEMENTS.values()))
TALENT_CAPS = (1, 1, 2, 4, 6, 8, 10)  # max talent level per ascension phase
SKIP_KEYS = {"Manekin", "Manekina"}


LV_RE = re.compile(r"L\s*[vV]\s*[.,:]?\s*(\d{1,2})\b")


def _talent_variants(region: Image.Image):
    """Binarizations of the talent list: plain luminance works on dark skies, the min channel
    on light (Cryo) skies, min(G, B) keeps the cyan of boosted levels."""
    a = np.asarray(region)
    rgb = lambda arr: Image.fromarray(arr.astype(np.uint8)).convert("RGB")
    return ((region, {}),
            (rgb(a.min(axis=2)), {"threshold": 150}),
            (region, {"text_height": 20, "line_px": 30}),
            (rgb(np.minimum(a[:, :, 1], a[:, :, 2])), {"threshold": 170}),
            (rgb(a.min(axis=2)), {"threshold": 170, "text_height": 20, "line_px": 30}),
            (rgb(a.min(axis=2)), {"threshold": 170}))


def _anchored_levels(lines: list[str], names: list[str]) -> list[int | None]:
    """Level under each talent name: first "Lv. N" between its name and the next one."""
    idx = []
    for n in names:
        scores = [fuzz.partial_ratio(n.lower(), l.lower()) if len(l) >= 4 else 0 for l in lines]
        best = max(range(len(lines)), key=scores.__getitem__, default=None)
        idx.append(best if best is not None and scores[best] >= 75 else None)
    out: list[int | None] = []
    for i in idx:
        lvl = None
        if i is not None:
            nxt = min([j for j in idx if j is not None and j > i] + [len(lines)])
            for l in lines[i:nxt]:
                m = LV_RE.search(l)
                if m and 1 <= int(m.group(1)) <= 15:
                    lvl = int(m.group(1))
                    break
        out.append(lvl)
    return out


def _element(text: str) -> str:
    hit = process.extractOne(text.strip(), ELEMENT_NAMES, scorer=fuzz.partial_ratio, score_cutoff=80)
    return hit[0] if hit else ""


class CharacterScanner:
    def __init__(self, s: Session, data: GameData):
        self.s, self.data = s, data
        self.c = s.cfg["character"]
        self.problems: list[str] = []
        self.results: list[dict] = []  # filled as we go, so an aborted scan keeps them

    def read_header(self):
        """(text, entry, element). The name comes from the Attributes panel (one line), the
        element from the top-left header ('Hydro / Sangonomiya Kokomi', wrapped when long)."""
        for _ in range(4):
            name = ocr.read(self.s.grab(self.c["name_panel"]), psm=7, light_text=True)
            header = " ".join(ocr.read_lines(self.s.grab(self.c["name"]), psm=6, light_text=True))
            txt = f"{name} | {header}"
            elem = next((e for w in re.findall(r"[A-Za-z]+", header) if (e := _element(w))), "")
            entry = (self.data.characters.match(name, cutoff=75) or self.data.characters.search_in(name)
                     or self.data.characters.search_in(header.split("/", 1)[-1]))
            if entry:
                return txt, entry, elem
            time.sleep(0.3)
        return txt, None, ""

    def _on_attributes(self) -> bool:
        """'Level N / M' is only shown by the Attributes tab."""
        level, _ = parse_level_cap(ocr.read(self.s.grab(self.c["level"]), psm=7, light_text=True))
        return level is not None

    def _open_tab(self, tab: str, wait: float, ready) -> bool:
        """Click a tab and check it really opened: clicks can get lost in the stream."""
        for _ in range(3):
            self.s.click(*self.c[tab])
            time.sleep(wait)
            if ready():
                return True
        return False

    def read_level(self) -> tuple[int | None, int | None]:
        for attempt in range(3):
            if attempt:
                self.s.click(*self.c["tab_attributes"])  # in case the tab click was lost
                time.sleep(self.s.t["ui"])
            level, cap = parse_level_cap(ocr.read(self.grab_still(self.c["level"], 3), psm=7, light_text=True))
            if level:
                return level, cap
        return None, None

    def _node_states(self, img) -> list[bool]:
        """Activated = bright ring around the node (gold for Geo, cyan for Hydro...)."""
        s, c = self.s, self.c
        a = np.asarray(img, dtype=np.float32)
        r0, r1 = (r * s.frame.sy for r in c["constellation_ring"])
        n = int(r1) + 1
        yy, xx = np.mgrid[-n:n + 1, -n:n + 1]
        ring = (np.hypot(yy, xx) >= r0) & (np.hypot(yy, xx) < r1)
        states = []
        for x, y in c["constellation_nodes"]:
            px, py = round(x * s.frame.sx), round(y * s.frame.sy)
            states.append(a[py - n:py + n + 1, px - n:px + n + 1][ring].mean() >= c["constellation_ring_min_lum"])
        return states

    def read_constellation(self, label: str) -> int:
        """Two identical readings after the tab animation; activated nodes must be C1..Ck."""
        s, t = self.s, self.s.t
        if not self._open_tab("tab_constellation", t["constellation_wait"], lambda: not self._on_attributes()):
            self.problems.append(f"{label}: Constellation tab not opened, constellation set to 0")
            return 0
        prev = None
        for _ in range(6):
            img = s.grab()
            states = self._node_states(img)
            count = states.index(False) if False in states else 6
            if states == [True] * count + [False] * (6 - count) and count == prev:
                self.s.save_debug(img.resize((img.width // 2, img.height // 2)), f"{label}_constellation.png")
                return count
            prev = count
            time.sleep(0.4)
        self.s.save_debug(img, f"{label}_constellation.png")
        self.problems.append(f"{label}: uncertain constellation (read C{count}), check the debug image")
        return count

    def grab_still(self, ref_rect, frames: int = 4, interval: float = 0.2) -> Image.Image:
        """Pixel-wise minimum of a few captures: the background particles (sparkles that can
        turn a '1' into a '4') move, the white text does not."""
        shots = []
        for i in range(frames):
            if i:
                time.sleep(interval)
            shots.append(np.asarray(self.s.grab(ref_rect)))
        return Image.fromarray(np.minimum.reduce(shots))

    def read_talents(self, avatar_id: str, constellation: int, ascension: int, label: str) -> dict:
        """Each "Lv. N" is tied to its talent name (known from the game data), so a missed
        line can never shift the others; several binarizations vote on each level."""
        s = self.s
        layout = self.data.talent_layout(avatar_id)
        boost = {k: 3 * ((constellation >= 3 and layout["c3"] == k) + (constellation >= 5 and layout["c5"] == k))
                 for k in ("auto", "skill", "burst")}
        cap = TALENT_CAPS[min(ascension, 6)]  # a displayed level above cap + boost is a misread
        def names_shown() -> bool:
            lines = [l.lower() for l in ocr.read_lines(s.grab(self.c["talent_list"]), psm=6, light_text=True)
                     if len(l) >= 4]
            best = [max((fuzz.partial_ratio(n.lower(), l) for l in lines), default=0) for n in layout["names"]]
            return sum(b >= 75 for b in best) >= 2 or max(best) >= 85

        if not self._open_tab("tab_talents", s.t["talents_wait"], names_shown):
            self.problems.append(f"{label}: Talents tab not opened")
        votes: list[list[int]] = [[], [], []]
        for _ in range(2):
            region = self.grab_still(self.c["talent_list"])
            for img, kw in _talent_variants(region):
                lines = ocr.read_lines(img, psm=6, light_text=True, **kw)
                for k, (name, lvl) in enumerate(zip(boost, _anchored_levels(lines, layout["names"]))):
                    if lvl is not None and lvl <= cap + boost[name]:
                        votes[k].append(lvl)
                if all(len(v) >= 2 and v.count(max(v, key=v.count)) >= 2 for v in votes):
                    break  # every talent confirmed by two readings
            if all(votes):
                break
            time.sleep(0.4)
        s.save_debug(region, f"{label}_talents.png")
        levels = {}
        for name, v in zip(("auto", "skill", "burst"), votes):
            best = max(v, key=v.count) if v else 1  # ties: the earliest (best) variant
            if not v:
                self.problems.append(f"{label}: unreadable '{name}' talent level, set to 1")
            elif len(v) == 1 or (len(set(v)) > 1 and v.count(best) <= len(v) / 2):
                self.problems.append(f"{label}: uncertain '{name}' talent (readings {v}), {best} kept")
            levels[name] = best
        # The game shows talent levels including the +3 from C3/C5; GOOD wants base levels.
        for cons, boosted in ((3, layout["c3"]), (5, layout["c5"])):
            if constellation >= cons and boosted and levels[boosted] > 3:
                levels[boosted] -= 3
        return levels

    def run(self, max_chars: int = 150) -> list[dict]:
        s, t = self.s, self.s.t
        out = self.results
        seen: list[str] = []
        misses = repeats = 0
        known_streak = False
        for i in range(max_chars):
            s.click(*self.c["tab_attributes"])
            time.sleep(t["ui"] if i == 0 else t["ui"] / 2)
            txt, entry, elem = self.read_header()
            if entry is None:
                misses += 1
                self.problems.append(f"unrecognized character: {txt!r}")
                if misses >= 4:
                    break
            else:
                ident = entry.key + elem if entry.key == "Traveler" else entry.key
                if seen and ident == seen[-1]:
                    # Same character right after itself: the click on the arrow was lost.
                    repeats += 1
                    if repeats >= 3:
                        self.problems.append("the 'next' arrow no longer responds: scan stopped")
                        break
                    s.click(*self.c["next"])
                    time.sleep(t["next_character"])
                    continue
                repeats = 0
                if ident in seen:
                    # Back on the first character = full circle. Any other known name may be a
                    # misread: only two known names in a row end the scan.
                    if ident == seen[0] or known_streak:
                        break
                    known_streak = True
                    self.problems.append(f"{ident} seen twice (misread name?), scan continues")
                    s.click(*self.c["next"])
                    time.sleep(t["next_character"])
                    continue
                known_streak = False
                seen.append(ident)
                if entry.rarity == 5 and entry.key not in SKIP_KEYS:
                    char = self.scan_one(entry, elem, txt)
                    if char:
                        out.append(char)
                        print(f"  {char['key']:<22} lv {char['level']:>2} A{char['ascension']} "
                              f"C{char['constellation']}  {char['talent']}")
                else:
                    print(f"  ({entry.name}: {entry.rarity}*, skipped)")
            s.click(*self.c["next"])
            time.sleep(t["next_character"])
        return out

    def scan_one(self, entry, elem: str, txt: str) -> dict | None:
        if entry.key == "Traveler":
            if not elem:
                self.problems.append(f"unreadable Traveler element: {txt!r}")
                return None
            key, avatar_id = "Traveler" + elem, self.data.traveler_ids.get(elem)
        else:
            key, avatar_id = entry.key, entry.extra["id"]
        level, cap = self.read_level()
        if level is None:
            self.problems.append(f"{key}: unreadable level")
            return None
        constellation = self.read_constellation(key)
        ascension = ascension_from_cap(cap)
        talents = self.read_talents(avatar_id, constellation, ascension, key)
        return {"key": key, "level": level, "constellation": constellation,
                "ascension": ascension, "talent": talents}
