"""Stat names, 5* artifact value tables and OCR-value validation/correction."""
from __future__ import annotations

import itertools
import re

from rapidfuzz import fuzz, process

# English in-game label -> (flat key, percent key)
STAT_LABELS: dict[str, tuple[str | None, str | None]] = {
    "HP": ("hp", "hp_"),
    "ATK": ("atk", "atk_"),
    "DEF": ("def", "def_"),
    "Elemental Mastery": ("eleMas", "eleMas"),
    "Energy Recharge": (None, "enerRech_"),
    "CRIT Rate": (None, "critRate_"),
    "CRIT DMG": (None, "critDMG_"),
    "Healing Bonus": (None, "heal_"),
    "Physical DMG Bonus": (None, "physical_dmg_"),
    "Anemo DMG Bonus": (None, "anemo_dmg_"),
    "Geo DMG Bonus": (None, "geo_dmg_"),
    "Electro DMG Bonus": (None, "electro_dmg_"),
    "Hydro DMG Bonus": (None, "hydro_dmg_"),
    "Pyro DMG Bonus": (None, "pyro_dmg_"),
    "Cryo DMG Bonus": (None, "cryo_dmg_"),
    "Dendro DMG Bonus": (None, "dendro_dmg_"),
}
_LABELS = list(STAT_LABELS)
_LABELS_NORM = [re.sub(r"[^a-z]", "", s.lower()) for s in _LABELS]

SLOTS = {
    "Flower of Life": "flower",
    "Plume of Death": "plume",
    "Sands of Eon": "sands",
    "Goblet of Eonothem": "goblet",
    "Circlet of Logos": "circlet",
}

# 5* substat roll values (one roll).
SUB_ROLLS_5 = {
    "hp": (209.13, 239.0, 268.88, 298.75),
    "atk": (13.62, 15.56, 17.51, 19.45),
    "def": (16.2, 18.52, 20.83, 23.15),
    "hp_": (4.08, 4.66, 5.25, 5.83),
    "atk_": (4.08, 4.66, 5.25, 5.83),
    "def_": (5.1, 5.83, 6.56, 7.29),
    "eleMas": (16.32, 18.65, 20.98, 23.31),
    "enerRech_": (4.53, 5.18, 5.83, 6.48),
    "critRate_": (2.72, 3.11, 3.5, 3.89),
    "critDMG_": (5.44, 6.22, 6.99, 7.77),
}

# 5* main stat value at level 0 and 20 (as displayed in game).
MAIN_5 = {
    "hp": (717, 4780), "atk": (47, 311),
    "hp_": (7.0, 46.6), "atk_": (7.0, 46.6), "def_": (8.7, 58.3),
    "eleMas": (28, 187), "enerRech_": (7.8, 51.8),
    "critRate_": (4.7, 31.1), "critDMG_": (9.3, 62.2), "heal_": (5.4, 35.9),
    "physical_dmg_": (8.7, 58.3),
    **{f"{e}_dmg_": (7.0, 46.6) for e in ("anemo", "geo", "electro", "hydro", "pyro", "cryo", "dendro")},
}

SLOT_MAINS = {
    "flower": {"hp"},
    "plume": {"atk"},
    "sands": {"hp_", "atk_", "def_", "eleMas", "enerRech_"},
    "goblet": {"hp_", "atk_", "def_", "eleMas", "physical_dmg_",
               *(f"{e}_dmg_" for e in ("anemo", "geo", "electro", "hydro", "pyro", "cryo", "dendro"))},
    "circlet": {"hp_", "atk_", "def_", "eleMas", "critRate_", "critDMG_", "heal_"},
}

ASCENSION_CAPS = (20, 40, 50, 60, 70, 80, 90)


def is_percent_key(key: str) -> bool:
    return key.endswith("_")


def stat_key(label: str, percent: bool) -> str | None:
    """Fuzzy-map an OCR'd stat label ('CRlT Rate') to a GOOD key."""
    words = re.findall(r"[A-Za-z]+", label)
    q = re.sub(r"[^a-z]", "", label.lower())
    if not q:
        return None
    hit = process.extractOne(q, _LABELS_NORM, scorer=fuzz.ratio, score_cutoff=70)
    if hit:
        name = _LABELS[hit[2]]
    elif words and words[0].upper() in ("HP", "ATK", "DEF"):  # short label + OCR noise
        name = words[0].upper()
    else:
        return None
    flat, pct = STAT_LABELS[name]
    return pct if (percent or flat is None) else flat


def _displayed(v: float, pct: bool) -> float:
    return round(v + 1e-9, 1) if pct else float(round(v + 1e-9))


def _roll_counts(key: str, max_rolls: int = 6) -> dict[float, set[int]]:
    """Displayed value -> possible numbers of rolls behind it."""
    rolls, pct = SUB_ROLLS_5[key], is_percent_key(key)
    out: dict[float, set[int]] = {}
    for n in range(1, max_rolls + 1):
        for combo in itertools.combinations_with_replacement(rolls, n):
            s = sum(combo)
            # The game rounds the exact float; allow both neighbours of a .x5 boundary.
            for eps in (-0.0005, 0, 0.0005):
                out.setdefault(_displayed(s + eps, pct), set()).add(n)
    return out


ROLL_COUNTS = {k: _roll_counts(k) for k in SUB_ROLLS_5}
VALID_SUBS = {k: set(v) for k, v in ROLL_COUNTS.items()}


def rolls_consistent(level: int, subs: list[dict], inactive: list[dict]) -> bool | None:
    """Do the substats fit a 5* artifact of this level? None when it cannot be told.

    Level 0: 3 or 4 single-roll substats (+1 unactivated when 3). Level 20: 4 substats
    totalling 8 or 9 rolls (3 or 4 initial + 5 upgrades).
    """
    counts = [ROLL_COUNTS.get(s["key"], {}).get(float(s["value"])) for s in subs]
    if not subs or any(c is None for c in counts):
        return None
    if level == 0:
        return len(subs) + len(inactive) == 4 and len(inactive) <= 1 and all(1 in c for c in counts)
    if level == 20:
        return len(subs) == 4 and not inactive and any(
            sum(p) in (8, 9) for p in itertools.product(*counts))
    return None


def parse_number(text: str) -> float | None:
    """First number of the text: '4,780' -> 4780, '46.6%' -> 46.6."""
    t = text.translate(str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "i": "1",
                                      "S": "5", "B": "8"}))
    m = re.search(r"\d{1,3}(?:,\d{3})+|\d+(?:[.,]\d+)?", t)
    if not m:
        return None
    s = m.group()
    s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+", s) else s.replace(",", ".")
    return float(s)


def fix_substat_value(key: str, raw: str) -> tuple[float | None, bool]:
    """Return (value, trusted). Repairs typical OCR errors using the roll tables."""
    pct = is_percent_key(key)
    v = parse_number(raw)
    valid = VALID_SUBS.get(key)
    if v is None or valid is None:
        return v, False
    if v in valid:
        return v, True
    digits = re.sub(r"\D", "", raw.translate(str.maketrans("IlLi|Oo", "1111100")))
    cands = []
    if pct and digits:
        # Lost decimal point: "39" -> 3.9, "117" -> 11.7
        cands.append(float(digits[:-1] + "." + digits[-1]) if len(digits) > 1 else float(digits))
        # "+11.7%" read as "t1.7%": the '+1' became a letter
        cands.append(float("1" + digits[:-1] + "." + digits[-1]))
    elif digits:
        cands.append(float("1" + digits))
    # Single-digit confusions (8/6/3/9/0, 1/7)
    swaps = {"8": "630", "6": "85", "3": "8", "9": "0", "0": "98", "1": "7", "7": "1", "5": "6"}
    s = raw.replace(",", "").translate(str.maketrans("IlLi|Oo", "1111100"))
    for i, ch in enumerate(s):
        for alt in swaps.get(ch, ""):
            n = parse_number(s[:i] + alt + s[i + 1:])
            if n is not None:
                cands.append(n)
    for c in cands:
        if c in valid:
            return c, True
    return v, False


def check_main(key: str, level: int, value: float | None) -> bool:
    """True when the OCR'd main stat value matches the 5* table (levels 0 and 20 only)."""
    if value is None or key not in MAIN_5 or level not in (0, 20):
        return False
    expected = MAIN_5[key][0 if level == 0 else 1]
    return abs(expected - value) <= (0.15 if is_percent_key(key) else 1.5)


LEVEL_CAPS = ASCENSION_CAPS + (95, 100)


def parse_level_cap(text: str) -> tuple[int | None, int | None]:
    """'Level 90 / 90', 'Lv. 70/80' -> (level, cap); tolerates a stray glyph glued to the level
    ('907/90', from sparkles in the character screen background)."""
    nums = re.findall(r"\d+", text.replace("O", "0").replace("o", "0"))
    if len(nums) < 2:
        return None, None
    cap_s = nums[-1]
    for cap in (int(cap_s), int(cap_s[:2]) if len(cap_s) > 2 else None):
        if cap not in LEVEL_CAPS:
            continue
        prev = max([c for c in LEVEL_CAPS if c < cap], default=1)
        raw = nums[-2]
        for lvl in (int(raw), int(raw[:-1]) if len(raw) > 1 else None, int(raw[1:]) if len(raw) > 1 else None):
            if lvl is not None and prev <= lvl <= cap:
                return lvl, cap
    return None, None


def ascension_from_cap(cap: int) -> int:
    """'Lv. 70/80' -> 5. Caps above 90 (post-90 levels) keep ascension 6."""
    if cap >= 90:
        return 6
    return ASCENSION_CAPS.index(cap) if cap in ASCENSION_CAPS else max(
        i for i, c in enumerate(ASCENSION_CAPS) if c <= cap)
