"""Parse an item card (the detail panel on the right of the inventory) into GOOD objects.

Pure image -> dict functions, so they can be tested on saved screenshots.
"""
from __future__ import annotations

import re

import numpy as np
from PIL import Image
from rapidfuzz import fuzz

from . import ocr
from .gamedata import GameData
from .stats import (SLOTS, SLOT_MAINS, ascension_from_cap, check_main, fix_substat_value,
                    parse_level_cap, parse_number, rolls_consistent, stat_key)


class Card:
    """A card capture normalized to the reference card size, with helpers to crop sub-regions."""

    def __init__(self, img: Image.Image, cfg: dict):
        self.cfg = cfg
        w, h = cfg["card"]["size"]
        self.scale = img.height / h  # source px per reference px (for OCR text-height hints)
        self.img = img.resize((w, h), Image.LANCZOS) if img.size != (w, h) else img
        self.shift = 0
        if self._sanctified():
            self.shift = cfg["card"]["sanctify_shift"]

    def crop(self, name: str, shifted: bool = False) -> Image.Image:
        x, y, w, h = self.cfg["card"][name]
        if shifted:
            y += self.shift
        return self.img.crop((x, y, x + w, y + h))

    def mean_rgb(self, name: str, shifted: bool = False) -> np.ndarray:
        return np.asarray(self.crop(name, shifted), dtype=np.float32).reshape(-1, 3).mean(axis=0)

    def _sanctified(self) -> bool:
        ref = np.array(self.cfg["card"]["sanctify_color"], dtype=np.float32)
        return bool(np.all(np.abs(self.mean_rgb("sanctify_probe") - ref) < 25))

    def rarity(self) -> int:
        """Name plate colour (gradient: compare against its dark and light shades)."""
        c = self.mean_rgb("name_plate")
        best, dist = 0, 1e9
        for r, refs in self.cfg["rarity_colors"].items():
            for ref in refs:
                d = float(((c - np.array(ref)) ** 2).sum())
                if d < dist:
                    best, dist = int(r), d
        return best if dist < 45 ** 2 * 3 else 0  # 0 = not an item card / empty

    def locked(self, region: str = "lock") -> bool:
        """Locked = dark square button with a salmon padlock (Inventory_Kamera / GO scanner colours)."""
        a = np.asarray(self.crop(region, region == "lock"), dtype=np.int16).reshape(-1, 3)
        for ref, min_share in self.cfg["lock_colors"]:
            if np.all(np.abs(a - np.array(ref)) < 28, axis=1).mean() >= min_share:
                return True
        return False

    def equipped(self, data: GameData, problems: list[str]) -> str:
        txt = ocr.read(self.crop("equipped"), psm=7, light_text=False)
        m = re.search(r"quipped\s*[:;]?\s*(.+)", txt, re.I)
        if not m:
            return ""
        e = data.characters.match(m.group(1), cutoff=75) or data.characters.search_in(m.group(1))
        if e is None:
            problems.append(f"unknown character {m.group(1).strip()!r} (renamed character? "
                            "add it to name_aliases in config.json)")
        return e.key if e else ""


def _badge(region: Image.Image) -> Image.Image:
    """Tighten a crop to the dark rounded badge it contains ('+20', 'Lv. 90/90')."""
    dark = np.asarray(region.convert("L")) < 110
    if dark.mean() > 0.15:
        rows, cols = np.nonzero(dark.mean(axis=1) > 0.3)[0], np.nonzero(dark.mean(axis=0) > 0.3)[0]
        if len(rows) and len(cols):
            return region.crop((cols.min(), rows.min(), cols.max() + 1, rows.max() + 1))
    return region


def _level_from_badge(card: Card) -> int | None:
    """'+20' badge. The digit whitelist makes Tesseract drop thin digits ('+19' -> '+'),
    so read freely first and fall back to the whitelist."""
    region = _badge(card.crop("level", True))
    for wl, line_px in ((None, 36), (None, 60), ("+0123456789", 36)):
        txt = ocr.read(region, psm=7, whitelist=wl, text_height=region.height * 0.6, line_px=line_px,
                       light_text=True)
        m = re.search(r"(\d{1,2})", txt.replace(" ", "").translate(str.maketrans("OoIl", "0011")))
        if not m:
            continue
        lvl = int(m.group(1))
        if lvl > 20 and str(lvl).startswith("4"):  # '+' read as '4'
            lvl = int(str(lvl)[1:])
        if 0 <= lvl <= 20:
            return lvl
    return None


# Main stat value: big bold digits; Tesseract reads them better slightly widened and scaled
# down (it still merges the "71" of "717" into an "M", hence the substat roll check below).
MAIN_VALUE_PASSES = ({"stretch": 1.3, "line_px": 28}, {"stretch": 1.0, "line_px": 36})


def _main_level(card: Card, main: str) -> tuple[int | None, str]:
    """Level (0 or 20) whose table value matches the OCR'd main stat value."""
    region = card.crop("main_value")
    txt = ""
    for p in MAIN_VALUE_PASSES:
        r = region.resize((round(region.width * p["stretch"]), region.height), Image.LANCZOS)
        txt = ocr.read(r, psm=7, whitelist="0123456789.,%", light_text=True, text_height=40,
                       line_px=p["line_px"])
        value = parse_number(txt)
        for lv in (0, 20):
            if check_main(main, lv, value):
                return lv, txt
    return None, txt


BODY_SCALES = ({"text_height": 24, "line_px": 30}, {}, {"text_height": 24, "line_px": 44})

# Value chars include the usual OCR confusions for digits (I/l/| for 1, O for 0...).
SUB_RE = re.compile(r"^[^A-Za-z]*([A-Za-z][A-Za-z .]*?)\s*[+t]\s*(\d[\dIl|iOoSB.,]*|[Il|][\dIl|iOoSB.,]+)\s*(%?)(.*)$")


def _parse_substats(lines: list[str]) -> tuple[list[dict], list[dict], list[str]]:
    subs, inactive, problems = [], [], []
    for line in lines:
        if line.rstrip().endswith(":") or "piece" in line.lower():
            break  # set name / set bonuses
        m = SUB_RE.match(line.replace("#", "+"))
        if not m:
            if "unactivated" in line.lower() and subs:  # "(unactivated)" wrapped onto its own line
                inactive.append(subs.pop())
            continue
        label, raw, pct, rest = m.groups()
        key = stat_key(label, bool(pct))
        if key is None:
            continue
        value, ok = fix_substat_value(key, raw + pct)
        if not ok:
            problems.append(f"doubtful substat: '{line}'")
        if value is not None and not key.endswith("_"):
            value = int(value)
        entry = {"key": key, "value": value}
        if "unactivated" in rest.lower() or "unactivated" in line.lower():
            inactive.append(entry)
        else:
            subs.append(entry)
        if len(subs) + len(inactive) == 4:
            break
    return subs, inactive, problems


def _decide_level(card: Card, main: str | None, subs: list[dict], inactive: list[dict],
                  problems: list[str]) -> int | None:
    """Badge first, cross-checked by the substat rolls and the main stat value.

    A level 0/20 that both checks contradict is rejected (None), so a '+20' misread as
    '+0' can never be written with the wrong level.
    """
    badge = _level_from_badge(card)
    main_lv, main_txt = _main_level(card, main) if main else (None, "")
    if badge is not None and badge not in (0, 20):
        return badge  # filtered out by the caller anyway
    if badge is not None:
        if rolls_consistent(badge, subs, inactive) is not False or main_lv == badge:
            return badge
        if main_lv is not None and rolls_consistent(main_lv, subs, inactive):
            problems.append(f"level read as +{badge}, corrected to +{main_lv} (main stat value and rolls)")
            return main_lv
        problems.append(f"level +{badge} inconsistent with the substats and the value {main_txt!r}")
        return None
    if main_lv is not None and rolls_consistent(main_lv, subs, inactive) is not False:
        return main_lv
    return None


def parse_artifact(img: Image.Image, cfg: dict, data: GameData) -> dict:
    """Returns {"rarity", "level", "artifact" (GOOD dict or None), "problems": [...]}."""
    card = Card(img, cfg)
    out: dict = {"rarity": card.rarity(), "level": None, "artifact": None, "problems": []}
    if out["rarity"] != 5:
        return out

    slot_txt = ocr.read(card.crop("slot"), psm=7, light_text=True)
    slot = None
    best = 0
    for name, key in SLOTS.items():
        s = fuzz.partial_ratio(name.lower(), slot_txt.lower())
        if s > best and s >= 70:
            slot, best = key, s

    if slot == "flower":
        main = "hp"
    elif slot == "plume":
        main = "atk"
    else:
        main_txt = ocr.read(card.crop("main_name"), psm=7, light_text=True)
        main = stat_key(main_txt, True)
        if slot and main not in SLOT_MAINS[slot]:
            out["problems"].append(f"main stat '{main_txt}' inconsistent with {slot}")

    # A 5* card always lists 4 substat lines (the 4th may be "(unactivated)"). Tesseract trips
    # on different lines depending on scale, so retry other scales until all 4 read cleanly.
    body = card.crop("body", True)
    best = None
    for scale in BODY_SCALES:
        lines = ocr.read_lines(body, psm=6, light_text=False, **scale)
        subs, inactive, problems = _parse_substats(lines)
        score = (len(subs) + len(inactive) == 4 and not problems, len(subs) + len(inactive), -len(problems))
        if best is None or score > best[0]:
            best = (score, lines, subs, inactive, problems)
        if score[0]:
            break
    _, lines, subs, inactive, problems = best
    out["problems"] += problems

    level = _decide_level(card, main, subs, inactive, out["problems"])
    out["level"] = level

    set_entry = None
    for line in [l for l in lines if l.rstrip().endswith(":")] + lines:  # the set line ends with ':'
        set_entry = data.sets.match(line.rstrip(": ."), cutoff=85)
        if set_entry:
            break
    if set_entry is None:
        set_entry = data.sets.search_in(" ".join(lines), cutoff=90)

    if slot is None:
        out["problems"].append(f"unreadable slot: {slot_txt!r}")
    if set_entry is None:
        out["problems"].append("set not found")
    if level is None and not any(p.startswith("level") for p in out["problems"]):
        out["problems"].append("unreadable level")
    if not subs:
        out["problems"].append("no substat read")

    art = {
        "setKey": set_entry.key if set_entry else "",
        "slotKey": slot or "",
        "level": level if level is not None else 0,
        "rarity": 5,
        "mainStatKey": main or "",
        "location": card.equipped(data, out["problems"]),
        "lock": card.locked(),
        "substats": subs,
    }
    if inactive:
        art["unactivatedSubstats"] = inactive
    out["artifact"] = art
    return out


def parse_weapon(img: Image.Image, cfg: dict, data: GameData, min_rarity: int = 5) -> dict:
    card = Card(img, cfg)
    out: dict = {"rarity": card.rarity(), "weapon": None, "problems": []}
    if out["rarity"] < min_rarity:
        return out
    name_txt = ocr.read(card.crop("name_plate"), psm=7, light_text=True)
    entry = data.weapons.match(name_txt, cutoff=80)
    if entry is None:
        out["problems"].append(f"unknown weapon: {name_txt!r}")
    elif entry.rarity != out["rarity"]:
        out["problems"].append(f"{entry.name} is {entry.rarity}* but the card looks {out['rarity']}*")
        entry = None

    region = _badge(card.crop("weapon_level"))
    lv_txt = ocr.read(region, psm=7, light_text=True, text_height=region.height * 0.6)
    level, cap = parse_level_cap(lv_txt)
    if level is None or cap > 90:
        out["problems"].append(f"unreadable level: {lv_txt!r}")
        level = cap = None

    # Gold text on cream: the blue channel gives the best contrast.
    ref_txt = ocr.read(card.crop("weapon_refinement"), psm=7, light_text=False, channel="B")
    m = re.search(r"Rank\s*([1-5])", ref_txt, re.I) or re.search(r"([1-5])\s*$", ref_txt)
    refinement = int(m.group(1)) if m else None
    if refinement is None:
        out["problems"].append(f"unreadable refinement: {ref_txt!r}")

    out["weapon"] = {
        "key": entry.key if entry else "",
        "level": level or 1,
        "ascension": ascension_from_cap(cap) if cap else 0,
        "refinement": refinement or 1,
        "location": card.equipped(data, out["problems"]),
        "lock": card.locked("weapon_lock"),
    }
    return out
