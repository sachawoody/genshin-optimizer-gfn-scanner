"""Genshin Impact scanner (GeForce NOW) -> GOOD file for Genshin Optimizer.

    python scan.py info                     detected GFN window and game area
    python scan.py capture                  F9 in game = capture saved to captures/
    python scan.py overlay IMAGE            draws the regions read on a capture (calibration)
    python scan.py parse IMAGE... --kind artifact|weapon   (captures or cards from --save-images)
    python scan.py artifacts | weapons | characters [--out good.json]

Emergency stop during a scan: move the mouse or press End.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from gscan import layout, ocr, winutil
from gscan.cards import Card, parse_artifact, parse_weapon
from gscan.gamedata import GameData
from gscan.inventory import Grid, InventoryScanner
from gscan.layout import Frame
from gscan.session import Session
from gscan.winutil import Rect

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"


def load_cfg() -> dict:
    cfg = layout.load(CONFIG)
    ocr.setup(cfg["tesseract_cmd"])
    return cfg


# --------------------------------------------------------------------------- output

def write_good(path: Path, section: str, items: list, merge: bool) -> None:
    doc = {"format": "GOOD", "version": 3, "source": "genshin-scan"}
    if merge and path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("format") == "GOOD":
            doc.update({k: v for k, v in old.items() if k in ("characters", "artifacts", "weapons")})
    if section == "characters" and merge:
        # Characters have a unique key: update them one by one (a partial rescan keeps the rest).
        by_key = {c["key"]: c for c in doc.get("characters", [])}
        by_key.update({c["key"]: c for c in items})
        items = list(by_key.values())
    doc[section] = items
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n-> {len(items)} {section} written to {path}")


def output_path(args, aborted: bool) -> Path:
    """An interrupted scan goes to a separate file so it never replaces a complete section."""
    out = Path(args.out)
    return out.with_name(out.stem + "_partial" + out.suffix) if aborted else out


def countdown(s: Session, seconds: int = 3) -> None:
    s.focus()
    for i in range(seconds, 0, -1):
        print(f"  starting in {i}s (hands off the mouse; End or moving the mouse = stop)")
        time.sleep(1)


# --------------------------------------------------------------------------- scans

def scan_inventory(kind: str, args) -> None:
    cfg = load_cfg()
    data = GameData(cfg["name_aliases"])
    debug = ROOT / "debug" / f"{kind}_{datetime.now():%Y%m%d_%H%M%S}"
    s = Session(cfg, cfg["window_title"], debug, args.save_images)
    print(s.describe())
    levels = {int(x) for x in args.levels.split(",")} if kind == "artifacts" else None
    parse = parse_artifact if kind == "artifacts" else parse_weapon
    scanner = InventoryScanner(s, kind)
    jobs = []

    min_rarity = getattr(args, "min_rarity", 5)

    def on_card(img: Image.Image, i: int, same: bool):
        rarity = Card(img, cfg).rarity()
        if args.save_images:
            s.save_debug(img, f"{i:04d}.png")
        if rarity < min_rarity:
            if rarity and not args.keep_going:
                print(f"  item {i + 1}: {rarity}* -> end of the {min_rarity}*+ items (sorted by quality)")
                return False
            return True
        extra = (min_rarity,) if kind == "weapons" else ()
        jobs.append((i, img, same, scanner.submit(parse, img, cfg, data, *extra)))
        if (i + 1) % 20 == 0:
            print(f"  {i + 1} items read")
        return True

    countdown(s)
    aborted = False
    try:
        n = scanner.run(on_card, max_items=args.max or 5000)
        print(f"Scan finished: {n} items visited")
    except winutil.Aborted as e:
        aborted = True
        print(f"\n[!] Scan interrupted ({e}): the items already read are kept")

    items, problems = [], []
    duplicates = 0
    for i, img, same, fut in jobs:
        res = fut.result()
        obj = res.get("artifact") or res.get("weapon")
        bad = list(res["problems"])
        # Unchanged card even after a second click: the click was lost, unless it is a real
        # duplicate (only plausible for an unequipped weapon).
        lost_click = same and (kind == "artifacts" or not obj or bool(obj["location"]))
        if lost_click:
            bad.append("card identical to the previous one -> lost click, rescan this item")
        elif same:
            duplicates += 1  # identical unequipped weapons (common for 3* and 4*)
        if kind == "artifacts":
            if res["level"] is not None and res["level"] not in levels:
                continue
            critical = not obj or not obj["setKey"] or not obj["slotKey"] or not obj["mainStatKey"] \
                or res["level"] is None or not obj["substats"]
        else:
            critical = not obj or not obj["key"]
        critical = critical or lost_click
        if bad:
            p = s.save_debug(img, f"problem_{i:04d}.png")
            problems.append(f"item {i + 1}{' (EXCLUDED)' if critical else ''}: {'; '.join(bad)} [{p.name}]")
        if not critical:
            items.append(obj)
    if duplicates:
        print(f"\n{duplicates} unequipped weapon(s) identical to the previous one, kept as duplicates")
    if problems:
        print(f"\n{len(problems)} item(s) to check (images in {debug}):")
        for p in problems:
            print("  - " + p)
    write_good(output_path(args, aborted), kind, items, not args.no_merge and not aborted)


def scan_characters(args) -> None:
    from gscan.characters import CharacterScanner
    cfg = load_cfg()
    data = GameData(cfg["name_aliases"])
    s = Session(cfg, cfg["window_title"], ROOT / "debug" / "characters")
    print(s.describe())
    countdown(s)
    sc = CharacterScanner(s, data)
    try:
        sc.run(max_chars=args.max or 200)
    except winutil.Aborted as e:
        print(f"\n[!] Scan interrupted ({e}): the characters already read are kept")
    chars = sc.results
    if any(c["key"] == "Tartaglia" for c in chars):
        print("[!] Tartaglia gives +1 Normal Attack level to the party: take Tartaglia out of the active "
              "party before the scan, otherwise the party's Normal Attack levels are 1 too high.")
    for p in sc.problems:
        print("  - " + p)
    write_good(Path(args.out), "characters", chars, not args.no_merge)  # merged by key: safe when aborted


# --------------------------------------------------------------------------- calibration tools

class Offline:
    """Stand-in for Session when working on a saved screenshot."""

    def __init__(self, img: Image.Image, cfg: dict):
        self.cfg = cfg
        self.frame = Frame(Rect(0, 0, img.width, img.height), cfg["frame"])


def cmd_info(args) -> None:
    cfg = layout.load(CONFIG)
    winutil.set_dpi_aware()
    print("Visible windows containing 'GeForce' or 'Genshin':")
    for h, t in winutil.list_windows():
        if "geforce" in t.lower() or "genshin" in t.lower():
            print(f"  {t!r}")
    print(Session(cfg, cfg["window_title"], ROOT / "debug").describe())


def cmd_capture(args) -> None:
    cfg = layout.load(CONFIG)
    s = Session(cfg, cfg["window_title"], ROOT / "captures")
    print(s.describe())
    vk = 0x70 + int(args.key[1:]) - 1 if args.key.upper().startswith("F") else ord(args.key.upper())
    print(f"Press {args.key} in game to capture, Ctrl+C here to quit.")
    out = ROOT / "captures"
    out.mkdir(exist_ok=True)
    was_down = False
    while True:
        down = bool(winutil.user32.GetAsyncKeyState(vk) & 0x8000)
        if down and not was_down:
            time.sleep(0.25)  # let the key press reach the stream first
            img = winutil.grab(s.client)
            p = out / f"capture_{datetime.now():%Y%m%d_%H%M%S}.png"
            img.save(p)
            print(f"  {p.name} ({img.width}x{img.height})")
        was_down = down
        time.sleep(0.03)


def _rect(draw: ImageDraw.ImageDraw, f: Frame, r, color: str, label: str = "") -> None:
    x0, y0, x1, y1 = f.local(r)
    draw.rectangle((x0, y0, x1, y1), outline=color, width=2)
    if label:
        draw.text((x0 + 3, y0 + 2), label, fill=color)


def _point(draw: ImageDraw.ImageDraw, f: Frame, p, color: str, label: str = "") -> None:
    x, y = f.local((p[0], p[1], 0, 0))[:2]
    draw.ellipse((x - 6, y - 6, x + 6, y + 6), outline=color, width=3)
    if label:
        draw.text((x + 8, y - 6), label, fill=color)


def cmd_overlay(args) -> None:
    cfg = layout.load(CONFIG)
    img = Image.open(args.image).convert("RGB")
    off = Offline(img, cfg)
    f = off.frame
    out = img.copy()
    d = ImageDraw.Draw(out)
    d.rectangle(f.local((0, 0, layout.REF_W, layout.REF_H)), outline="white", width=1)
    if args.screen == "inventory":
        inv = cfg["inventory"]
        _rect(d, f, inv["grid_area"][args.tab], "yellow", "grid_area")
        g = Grid(off, args.tab)
        rows = g.detect_rows(img)
        print(f"{len(rows)} row(s) detected: " + ", ".join(f"y={r.top:.0f} cols={r.cols}" for r in rows))
        for r in rows or g.fallback_rows():
            for c in range(g.cols):
                _rect(d, f, (g.col_x(c) - g.tile_w / 2, r.top, g.tile_w, g.tile_h),
                      "lime" if c in r.cols else "red")
        cx, cy, cw, ch = inv["card"]
        _rect(d, f, inv["card"], "cyan", "card")
        k = cw / cfg["card"]["size"][0]
        card = Card(img.crop(f.local(inv["card"])), cfg)
        for name in ("name_plate", "slot", "main_name", "main_value", "level", "lock", "body",
                     "equipped", "sanctify_probe", "weapon_level", "weapon_refinement"):
            x, y, w, h = cfg["card"][name]
            if name in ("level", "lock", "body"):
                y += card.shift
            _rect(d, f, (cx + x * k, cy + y * k, w * k, h * k), "magenta", name)
        print(f"rarity read: {card.rarity()}*  locked: {card.locked()}  'sanctify' shift: {card.shift}")
    else:
        c = cfg["character"]
        _rect(d, f, c["name"], "cyan", "name")
        _rect(d, f, c["level"], "cyan", "level")
        _rect(d, f, c["constellation_probe"], "yellow", "probe")
        _rect(d, f, c["talent_level"], "yellow", "talent_level")
        for k in ("tab_attributes", "tab_constellation", "tab_talents", "next"):
            _point(d, f, c[k], "lime", k)
        for i, p in enumerate(c["constellation_nodes"]):
            _point(d, f, p, "magenta", f"C{i + 1}")
        for i, p in enumerate(c["talent_rows"]):
            _point(d, f, p, "orange", f"T{i + 1}")
    p = Path(args.image).with_name(Path(args.image).stem + "_overlay.png")
    out.save(p)
    print(f"-> {p}")


def cmd_parse(args) -> None:
    """Full screenshots, or card images saved by --save-images (portrait = already a card)."""
    cfg = load_cfg()
    data = GameData(cfg["name_aliases"])
    parse = parse_artifact if args.kind == "artifact" else parse_weapon
    for path in args.images:
        img = Image.open(path).convert("RGB")
        card = img if img.height > img.width else img.crop(Offline(img, cfg).frame.local(cfg["inventory"]["card"]))
        res = parse(card, cfg, data)
        if len(args.images) == 1:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            print(f"{Path(path).name}: {json.dumps(res.get('artifact') or res.get('weapon'), ensure_ascii=False)}"
                  + (f"  PROBLEMS: {res['problems']}" if res["problems"] else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    p = sub.add_parser("capture")
    p.add_argument("--key", default="F9")
    p = sub.add_parser("overlay")
    p.add_argument("image")
    p.add_argument("--screen", choices=["inventory", "character"], default="inventory")
    p.add_argument("--tab", choices=["artifacts", "weapons"], default="artifacts")
    p = sub.add_parser("parse")
    p.add_argument("images", nargs="+")
    p.add_argument("--kind", choices=["artifact", "weapon"], default="artifact")
    for name in ("artifacts", "weapons", "characters"):
        p = sub.add_parser(name)
        p.add_argument("--out", default=str(ROOT / "good.json"))
        p.add_argument("--no-merge", action="store_true", help="overwrite the file instead of updating it")
        p.add_argument("--max", type=int, default=0, help="maximum number of items/characters (test run)")
        if name != "characters":
            p.add_argument("--save-images", action="store_true", help="keep the capture of every item card")
            p.add_argument("--keep-going", action="store_true",
                           help="don't stop at the first item below the minimum rarity (inventory not sorted by quality)")
        if name == "weapons":
            p.add_argument("--min-rarity", type=int, default=3, choices=[1, 2, 3, 4, 5],
                           help="minimum rarity of the weapons kept (default 3)")
        if name == "artifacts":
            p.add_argument("--levels", default="0,20", help="levels kept (default 0,20)")
    args = ap.parse_args()
    if args.cmd in ("artifacts", "weapons"):
        scan_inventory(args.cmd, args)
    elif args.cmd == "characters":
        scan_characters(args)
    else:
        {"info": cmd_info, "capture": cmd_capture, "overlay": cmd_overlay, "parse": cmd_parse}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
