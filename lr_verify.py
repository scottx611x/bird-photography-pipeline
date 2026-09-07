#!/usr/bin/env python3
"""
lr_verify.py — did Lightroom actually do the thing?

lr_auto.py reports success from its own exit code, which says only that the
AppleScript ran. Twice now an import has printed "Imported: <folder>", exited
0, and ingested nothing — leaving auto-tone, denoise and export to operate on
whatever batch happened to still be open.

The one signal that can't lie is the screen: if photos really were imported,
Lightroom's filmstrip changes. These helpers turn a screenshot into a small
fingerprint and compare two of them. Kept free of Flask and of any Lightroom
plumbing so the logic can be tested directly.
"""

from io import BytesIO

# Bottom band of the window, excluding the right-hand tool rail: that is where
# the filmstrip lives, so it changes when the visible set of photos changes.
STRIP_TOP = 0.80
STRIP_RIGHT = 0.80
GRID = (48, 8)          # coarse enough to ignore cursor and hover highlights
CHANGED_AT = 8.0        # mean per-cell difference that counts as a real change


def fingerprint(image_bytes: bytes):
    """A short list of grey levels describing Lightroom's filmstrip.
    Returns None when there's nothing usable to compare."""
    if not image_bytes:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(BytesIO(image_bytes)).convert("L")
    except Exception:
        return None
    w, h = im.size
    if w < 50 or h < 50:
        return None
    strip = im.crop((0, int(h * STRIP_TOP), int(w * STRIP_RIGHT), h))
    return list(strip.resize(GRID, Image.LANCZOS).getdata())


def difference(before, after) -> float:
    """Mean per-cell difference between two fingerprints, or -1.0 if either is
    missing — callers must treat that as "unknown", never as "unchanged"."""
    if not before or not after or len(before) != len(after):
        return -1.0
    return sum(abs(a - b) for a, b in zip(before, after)) / len(before)


def changed(before, after, threshold: float = CHANGED_AT):
    """True / False / None (couldn't tell). None must not block the pipeline:
    a missing screenshot is a permissions problem, not a failed import."""
    d = difference(before, after)
    if d < 0:
        return None
    return d > threshold
