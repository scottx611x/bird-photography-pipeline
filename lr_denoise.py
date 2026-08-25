#!/usr/bin/env python3
"""
lr_denoise.py — best-effort automation of the one manual step: ticking
Denoise in Lightroom's Edit > Detail panel.

Lightroom exposes no accessibility children and has no Denoise menu command,
so this drives it by sight: screenshot, locate the checkbox, click it, then
screenshot again to confirm it actually changed. It never assumes success —
if anything is uncertain it exits non-zero so the caller can fall back to the
manual gate. Deliberately opt-in; nothing calls it automatically.

  python3 lr_denoise.py status   # report only, click nothing
  python3 lr_denoise.py enable   # tick Denoise if it isn't already
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

APP = "Adobe Lightroom"
CLICLICK = "/opt/homebrew/bin/cliclick"
LOG_W, LOG_H = 1512, 982          # logical points for a 3024x1964 retina panel

# The checkbox column in Lightroom's Edit panel, in logical points.
COL_X0, COL_X1 = 1213, 1233
COL_CENTRE = 1222
SCAN_TOP, SCAN_BOTTOM = 355, 640  # below the Detail header, above Sharpening
BOX_MIN, BOX_MAX = 8, 12          # border-to-border height, in logical points
CHECKED_THRESHOLD = 130           # inner mean: ~79 unchecked, ~183 checked


def _osa(script: str, timeout: int = 20) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True,
                       text=True, timeout=timeout)
    return (r.stdout or "").strip()


def activate():
    """Bring Lightroom forward. Other automation on this Mac steals focus, so
    do this immediately before any click — and tolerate the timeout that
    happens while Lightroom is busy applying Denoise."""
    try:
        _osa(f'tell application "{APP}" to activate', timeout=15)
    except subprocess.TimeoutExpired:
        pass
    import time
    time.sleep(1.2)          # let the window actually come forward before we look


def grab() -> Image.Image:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        path = tf.name
    subprocess.run(["/usr/sbin/screencapture", "-x", "-t", "png", path],
                   capture_output=True, timeout=30)
    im = Image.open(path).convert("L")
    # Normalise to logical points so coordinates map 1:1 and the pixel-run
    # thresholds below don't depend on the display's retina scale factor.
    im = im.resize((LOG_W, LOG_H), Image.LANCZOS)
    Path(path).unlink(missing_ok=True)
    return im


def find_checkbox(im: Image.Image):
    """Locate the first checkbox below the Detail header: a small square whose
    top and bottom borders show up as short runs of bright pixels. Returns its
    centre in logical points, or None."""
    def run_len(ly):
        """Longest unbroken bright run in the row — a checkbox border is one
        solid segment, whereas text and chevrons are scattered."""
        best = cur = 0
        for x in range(COL_X0, COL_X1):
            cur = cur + 1 if im.getpixel((x, ly)) > 110 else 0
            best = max(best, cur)
        return best

    runs = {ly: run_len(ly) for ly in range(SCAN_TOP, SCAN_BOTTOM)}
    for ly in range(SCAN_TOP, SCAN_BOTTOM - BOX_MAX - 1):
        if not (8 <= runs[ly] <= 14):
            continue
        if runs.get(ly - 2, 0) > 14:          # row above must be background
            continue
        # the matching bottom border sits 8-12 points lower
        for side in range(BOX_MIN, BOX_MAX + 1):
            if 8 <= runs.get(ly + side, 0) <= 14:
                return COL_CENTRE, ly + side // 2
    return None


def is_checked(im: Image.Image, centre) -> bool:
    px, py = centre
    vals = [im.getpixel((x, y))
            for x in range(px - 3, px + 4) for y in range(py - 3, py + 4)]
    return (sum(vals) / len(vals)) > CHECKED_THRESHOLD


def single_panel_mode_on():
    """Collapse the Edit panel to one section at a time so the Detail contents
    land at a stable position. Idempotent — reads the menu's checkmark first."""
    state = _osa(f'''
    tell application "System Events"
      tell process "{APP}"
        set ep to menu item "Edit Panels" of menu 1 of (menu bar item "View" of menu bar 1)
        set s to menu item "Single-Panel Mode" of menu 1 of ep
        try
          if (value of attribute "AXMenuItemMarkChar" of s) is not missing value then return "on"
        end try
        return "off"
      end tell
    end tell''')
    if state != "on":
        _osa(f'''
        tell application "System Events"
          tell process "{APP}"
            click menu item "Single-Panel Mode" of menu 1 of ¬
              (menu item "Edit Panels" of menu 1 of (menu bar item "View" of menu bar 1))
          end tell
        end tell''')
        return "enabled"
    return "already on"


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if _osa(f'tell application "System Events" to return (exists process "{APP}")') != "true":
        print("Lightroom is not running.")
        sys.exit(1)

    activate()
    if cmd == "enable":
        print(f"Single-Panel Mode: {single_panel_mode_on()}")

    box = find_checkbox(grab())
    if not box:
        print("Couldn't find the Denoise checkbox — is Edit > Detail expanded?")
        sys.exit(2)
    print(f"Denoise checkbox at {box[0]},{box[1]}")

    if is_checked(grab(), box):
        print("Denoise is already ON — nothing to do.")
        return
    if cmd != "enable":
        print("Denoise is OFF.")
        sys.exit(3)

    activate()
    subprocess.run([CLICLICK, f"c:{box[0]},{box[1]}"], capture_output=True, timeout=20)

    # Confirm by sight rather than trusting the click.
    import time
    for _ in range(10):
        time.sleep(1)
        if is_checked(grab(), box):
            print("Denoise is now ON (verified).")
            return
    print("Clicked, but the checkbox never showed as ticked — do it by hand.")
    sys.exit(4)


if __name__ == "__main__":
    main()
