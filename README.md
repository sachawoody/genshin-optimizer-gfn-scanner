# genshin-optimizer-gfn-scanner

Genshin Impact scanner for **GeForce NOW**: it reads the stream on screen and drives the mouse, then
writes a **GOOD** file to import into [Genshin Optimizer](https://frzyc.github.io/genshin-optimizer/).

What gets scanned:

- **5★ artifacts at level 0 and 20**: set, slot, main stat, substats (including the 4th "unactivated"
  one), equipped character, lock;
- **3★ to 5★ weapons**: level, ascension, refinement, equipped character, lock;
- **5★ characters**: level, ascension, constellation, talents (the +3 from C3/C5 is removed, as GOOD
  expects).

Requirements:

- Windows (screen capture and input go through the Win32 API);
- the game in **English**, streamed in **16:9** (calibrated and tested at 2560x1440);
- Python 3.10 or later, and Tesseract OCR.

## Installation

1. Install Tesseract OCR. On Windows, use the
   [UB Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki). The script finds it on the
   `PATH` or in its default install folders; otherwise set `tesseract_cmd` in `config.json`.
2. Create the project's virtual environment (the `scan` command uses it):

   ```
   py -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```

Character, weapon and set names are downloaded from [gi.yatta.moe](https://gi.yatta.moe) and cached in
`data_cache/`: new game content is picked up without changing the script.

## Usage

Run each scan from a terminal opened in this folder, once the right screen is shown in game. The script
brings the GeForce NOW window to the front and starts after 3 s.

**Emergency stop: move the mouse or press End.** For artifacts and weapons, what has already been read
is written to `good_partial.json`, leaving `good.json` untouched. Characters already read are updated
directly in `good.json`.

| Step | In-game screen | Command |
|---|---|---|
| Artifacts | Inventory → Artifacts, **sorted by Quality**, scrolled to the top | `.\scan artifacts` |
| Weapons | Inventory → Weapons, **sorted by Quality**, scrolled to the top | `.\scan weapons` |
| Characters | Character screen (C key), any character | `.\scan characters` |

- Each scan updates `good.json`: the artifacts or weapons section is replaced, the others are kept.
  Characters are updated one by one, so `--max 1` rescans the character on screen. `--no-merge` starts
  from an empty file, `--out other.json` writes to another file.
- Inventory scans stop at the first item below the minimum rarity (artifacts: below 5★, weapons: below
  3★), which relies on the Quality sort. Otherwise, `--keep-going` walks the whole inventory.
- `--min-rarity 4` (weapons) changes the minimum rarity kept; `--levels 0,4,20` (artifacts) the levels
  kept.
- Unequipped weapons identical to the previous one (wish duplicates) are kept and only counted. Each
  duplicate is confirmed by a second click, which adds about 3 s per duplicate.
- `--max 20` runs a short test.
- `--save-images` keeps the capture of every item card in `debug/`.

Measured durations: about 0.6 s per artifact or weapon, and 8 s per 5★ character.

At the end of a scan, doubtful items are listed and their card is saved in `debug/`:

- **EXCLUDED**: the item is not written (unreadable set, lost click...). Rescan it, or add it by
  hand in GO.
- no tag: the item is written, but a value was corrected or deserves a look.

### Importing into Genshin Optimizer

Database → Upload → choose `good.json`.

**Warning: GO deletes the artifacts and weapons that are missing from the file**, unless you tick the
option that keeps items absent from the import. The file only holds what is listed above (5★ artifacts
at levels 0 and 20, 3★ to 5★ weapons): tick it to keep the rest of your database.

Other GO behaviours worth knowing:

- an item equipped on a character missing from the file (a 4★, for instance) creates that character in
  GO at level 90 with talents at 9;
- the Traveler is saved as `Traveler<Element>`, with "Traveler" as the equipped location.

## Things to watch

- **Renamed characters**: the Traveler (and the Wanderer) show up under the name you gave them. Declare
  them in a `config.json` file next to `scan.py`:

  ```json
  { "name_aliases": { "MyTravelerName": "Traveler" } }
  ```

  An unknown character is reported as `unknown character 'X'`.
- **Tartaglia** gives +1 Normal Attack level to the whole active party: take Tartaglia out of the
  active party before `.\scan characters`.
- Don't touch the mouse or the game screen during a scan.
- The constellation and talent captures of each character are kept in `debug/characters/` for
  checking. **These captures show your UID**: don't share them.

## Recalibration (other resolution, UI update)

Positions are defined for a 1920x1080 image and scaled to the stream (`gscan/layout.py`). Any key can be
overridden in `config.json`, with the same structure. Tools:

- `.\scan info`: detected window and game area;
- `.\scan capture`: in game, F9 saves a capture to `captures/`;
- `.\scan overlay captures\x.png [--screen character] [--tab weapons]`: draws the regions that are read;
- `.\scan parse captures\x.png [--kind weapon]`: reads an item card offline (also accepts the cards saved
  by `--save-images`).

## Credits

- Screen layout values come from the 16:9 layout of
  [Inventory_Kamera](https://github.com/Andrewthe13th/Inventory_Kamera).
- Game data: [gi.yatta.moe](https://gi.yatta.moe) (formerly Ambr).
- Not affiliated with HoYoverse or NVIDIA.

## License

[MIT](LICENSE)
