"""Tesseract wrappers with the preprocessing that works on Genshin UI text."""
from __future__ import annotations

import os
import shutil

import numpy as np
import pytesseract
from PIL import Image, ImageOps

_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
]


def setup(cmd: str | None = None) -> None:
    for c in [cmd, shutil.which("tesseract"), *_CANDIDATES]:
        if c and os.path.exists(c):
            pytesseract.pytesseract.tesseract_cmd = c
            return
    raise SystemExit("Tesseract not found: install it or set 'tesseract_cmd' in config.json")


def otsu(gray: np.ndarray) -> int:
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = gray.size
    sum_all = np.dot(np.arange(256), hist)
    w_b = sum_b = 0.0
    best_t, best_var = 127, -1.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0 or w_b == total:
            continue
        sum_b += t * hist[t]
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / (total - w_b)
        var = w_b * (total - w_b) * (m_b - m_f) ** 2
        if var > best_var:
            best_t, best_var = t, var
    return best_t


def prepare(img: Image.Image, text_height: int | None = None, line_px: int = 36,
            light_text: bool | None = None, threshold: int | None = None,
            channel: str = "L") -> Image.Image:
    """Grayscale -> upscale -> binarize -> black text on white, with a margin.

    `text_height`: approximate glyph height in the source image; it is rescaled to `line_px`
    (Tesseract is most accurate around 30-40 px per line).
    `light_text`: True for white text on a dark/colored background; None = guess.
    """
    if channel == "L":
        g = img.convert("L")
    elif channel == "max":  # keeps colored text (green set names, blue boosted levels) bright
        g = Image.fromarray(np.asarray(img.convert("RGB")).max(axis=2).astype(np.uint8))
    else:
        g = img.convert("RGB").getchannel(channel)
    if text_height:
        f = line_px / max(1, text_height)
        if abs(f - 1) > 0.1:
            g = g.resize((max(1, round(g.width * f)), max(1, round(g.height * f))), Image.LANCZOS)
    a = np.asarray(g)
    t = otsu(a) if threshold is None else threshold
    binary = a > t
    if light_text is None:
        light_text = binary.mean() < 0.5  # few bright pixels -> they are the text
    text = binary if light_text else ~binary
    out = Image.fromarray(np.where(text, 0, 255).astype(np.uint8))
    return ImageOps.expand(out, border=12, fill=255)


def read(img: Image.Image, psm: int = 7, whitelist: str | None = None, **prep) -> str:
    cfg = f"--psm {psm} --oem 1"
    if whitelist:  # no spaces allowed: pytesseract splits the config string
        cfg += f" -c tessedit_char_whitelist={whitelist}"
    return pytesseract.image_to_string(prepare(img, **prep), lang="eng", config=cfg).strip()


def read_lines(img: Image.Image, psm: int = 6, **prep) -> list[str]:
    return [l.strip() for l in read(img, psm=psm, **prep).splitlines() if l.strip()]
