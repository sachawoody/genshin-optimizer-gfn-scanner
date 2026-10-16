"""In-game English names -> GOOD keys, built from the gi.yatta.moe (ex-Ambr) public API.

The lists are cached in data_cache/ and refreshed every few days, so characters,
weapons and sets released after this script was written are picked up automatically.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz, process

API = "https://gi.yatta.moe/api/v2/{lang}/{kind}"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data_cache"
CACHE_TTL = 3 * 24 * 3600

ELEMENTS = {"Wind": "Anemo", "Rock": "Geo", "Electric": "Electro", "Grass": "Dendro",
            "Water": "Hydro", "Fire": "Pyro", "Ice": "Cryo"}


def to_good_key(name: str) -> str:
    """Same rule as GO's nameToKey (plus accent folding):
    'Wolf's Gravestone' -> 'WolfsGravestone', 'Maréchaussée Hunter' -> 'MarechausseeHunter'."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-zA-Z0-9 ]", "", name)
    return "".join(w[:1].upper() + w[1:] for w in name.split(" ") if w)


def _fetch(kind: str, lang: str = "en") -> dict:
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{lang}-{kind.replace('/', '-')}.json"
    if path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL:
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        req = urllib.request.Request(API.format(lang=lang, kind=kind), headers={"User-Agent": "genshin-scan"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)["data"]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
    except Exception as e:
        if path.exists():
            print(f"[data] {kind}: download failed ({e}), using the cache")
            return json.loads(path.read_text(encoding="utf-8"))
        raise


@dataclass
class Entry:
    key: str
    name: str
    rarity: int
    extra: dict = field(default_factory=dict)


class NameIndex:
    """Fuzzy lookup of OCR'd names."""

    def __init__(self, entries: list[Entry]):
        self.entries = entries
        self._norm = [self.norm(e.name) for e in entries]

    @staticmethod
    def norm(s: str) -> str:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
        return re.sub(r"[^a-z0-9]+", "", s)

    def match(self, text: str, cutoff: float = 80) -> Entry | None:
        q = self.norm(text)
        if len(q) < 3:
            return None
        hit = process.extractOne(q, self._norm, scorer=fuzz.ratio, score_cutoff=cutoff)
        return self.entries[hit[2]] if hit else None

    def search_in(self, text: str, cutoff: float = 85) -> Entry | None:
        """Find a name contained somewhere in a longer OCR line."""
        q = self.norm(text)
        best, best_score = None, cutoff
        for e, n in zip(self.entries, self._norm):
            if len(n) < 3 or len(q) < 3:
                continue
            # Short names ("Ifa", "Aino") would be found inside any noisy text: whole match only.
            s = fuzz.partial_ratio(n, q) if 5 <= len(n) <= len(q) else fuzz.ratio(n, q)
            if s > best_score or (s == best_score and best and len(e.name) > len(best.name)):
                best, best_score = e, s
        return best


class GameData:
    def __init__(self, aliases: dict[str, str] | None = None):
        """`aliases`: names shown in game that differ from the official ones, e.g. the
        Traveler's or the Wanderer's custom name: {"MyTravelerName": "Traveler"}."""
        avatars = _fetch("avatar")["items"]
        weapons = _fetch("weapon")["items"]
        sets = _fetch("reliquary")["items"]

        chars: dict[str, Entry] = {}
        self.traveler_ids: dict[str, str] = {}  # element -> avatar id
        for aid, a in avatars.items():
            if a["name"] == "Traveler" or aid.startswith(("10000005", "10000007")):
                elem = ELEMENTS.get(a.get("element", ""), "")
                if elem:
                    self.traveler_ids.setdefault(elem, aid)
                continue
            key = to_good_key(a["name"])
            chars[key] = Entry(key, a["name"], a["rank"],
                               {"id": aid, "element": ELEMENTS.get(a.get("element", ""), "")})
        # GOOD uses "Traveler" as location (and Traveler<Element> as character key).
        for n in ("Traveler", "Aether", "Lumine"):
            chars[to_good_key(n)] = Entry("Traveler", n, 5)
        for shown, target in (aliases or {}).items():
            tgt = chars.get(to_good_key(target))
            if tgt is None:
                print(f"[config] alias {shown!r} -> {target!r}: unknown target character")
                continue
            chars[to_good_key(shown)] = Entry(tgt.key, shown, tgt.rarity, tgt.extra)
        self.characters = NameIndex(list(chars.values()))

        self.weapons = NameIndex([Entry(to_good_key(w["name"]), w["name"], w["rank"])
                                  for w in weapons.values() if not w.get("isWeaponSkin")])
        self.sets = NameIndex([Entry(to_good_key(s["name"]), s["name"], max(s.get("levelList") or [5]))
                               for s in sets.values()])

    def talent_layout(self, avatar_id: str) -> dict:
        """Which combat talent C3/C5 boost (+3), and the row of the burst in the Talents tab.

        Combat talents are listed as Normal Attack, Skill, [alternate sprint], Burst.
        """
        d = _fetch(f"avatar/{avatar_id}")
        talents = [t for t in d["talent"].values() if t.get("type") in (0, 1)]
        combat0 = [t for t in talents if t["type"] == 0]
        burst = next((t for t in talents if t["type"] == 1), None)
        names = {"auto": combat0[0]["name"] if combat0 else "",
                 "skill": combat0[1]["name"] if len(combat0) > 1 else "",
                 "burst": burst["name"] if burst else ""}
        cons = sorted(d.get("constellation", {}).values(), key=lambda c: c.get("id", 0))

        def boosted(idx: int) -> str | None:
            if len(cons) <= idx:
                return None
            # Wording varies ("Increases the Level of X by 3", "Increases Elemental Skill X Level
            # by 3", "Elemental Burst X increases by 3 Levels"): use the first sentence.
            desc = re.sub(r"<[^>]+>|\{/?LINK[^}]*\}", "", cons[idx]["description"])
            first = re.split(r"\\n|\n", desc)[0]
            if not re.search(r"\b3\b", first) or "level" not in first.lower():
                return None
            for word, k in (("Normal Attack", "auto"), ("Elemental Skill", "skill"), ("Elemental Burst", "burst")):
                if word in first:
                    return k
            for k in ("skill", "burst", "auto"):
                if names[k] and fuzz.partial_ratio(names[k].lower(), first.lower()) > 90:
                    return k
            return None

        return {"c3": boosted(2), "c5": boosted(4), "burst_row": len(combat0),
                "names": [names["auto"], names["skill"], names["burst"]]}
