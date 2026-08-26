#!/usr/bin/env python3
"""
lr_denoise.py — best-effort automation of the two manual Lightroom clicks:
Denoise (Edit > Detail) and dust removal (Remove tool > Distraction Removal >
Dust > Apply).

Lightroom exposes no accessibility children for its panels and has no menu
command for either setting, so the checkbox has to be found by sight. Sight
alone is not enough to be *safe*, though: if the wrong panel is showing, the
first checkbox found may be something else entirely — this once ticked
"Visualize spots" while reporting "Denoise is now ON". So every run:

  * pins the active tool through View > Edit Tools, which both reports and
    sets it, so we know which panel we're looking at before hunting;
  * re-reads menu-visible flags afterwards and undoes the click if one of
    those moved instead of the setting we wanted;
  * treats Lightroom's own "Applying Denoise" progress window as proof that
    Denoise specifically was toggled — that window also dims the panel, which
    is why a brightness check alone used to report a false failure.

Exits non-zero whenever it cannot prove what it did, so the caller falls back
to the manual gate. Nothing calls this automatically.

  python3 lr_denoise.py status | enable | dust-status | dust
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

APP = "Adobe Lightroom"
CLICLICK = "/opt/homebrew/bin/cliclick"
LOG_W, LOG_H = 1512, 982          # logical points for a 3024x1964 retina panel

COL_X0, COL_X1 = 1213, 1233       # the checkbox column in the right-hand panel
COL_CENTRE = 1222
BOX_MIN, BOX_MAX = 8, 12          # border-to-border height, in logical points
CHECKED_THRESHOLD = 130           # inner mean: ~79 unticked, ~183 ticked

# Denoise is the topmost checkbox in the Detail panel (Raw Details and
# Super Resolution follow it), so scan from above the fold and take the
# first hit — the panel's scroll position varies between runs.
DETAIL_SCAN = (215, 900)
DUST_SCAN = (600, 900)            # Distraction Removal sits low in the panel

# Menu-visible flags that must NOT move — if one does, we hit the wrong box.
EDIT_ICON = (1483, 154)      # sliders icon in the right-hand tool strip
ERASER_ICON = (1483, 262)    # eraser (Remove) icon

GUARD_FLAGS = ("Visualize spots", "Show Overlay", "Show Clipping")


def _osa(script: str, timeout: int = 20) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return ""


def _menu_path(item: str, sub: str) -> str:
    return (f'menu item "{item}" of menu 1 of (menu item "{sub}" of menu 1 of '
            f'(menu bar item "View" of menu bar 1))')


def menu_flag(item: str, sub: str = "Edit Tools") -> bool:
    got = _osa('tell application "System Events" to tell process "%s"\n'
               'try\n'
               'if (value of attribute "AXMenuItemMarkChar" of %s) '
               'is not missing value then return "on"\n'
               'end try\n'
               'return "off"\n'
               'end tell' % (APP, _menu_path(item, sub)))
    return got == "on"


def menu_click(item: str, sub: str = "Edit Tools"):
    _osa('tell application "System Events" to tell process "%s" to click %s'
         % (APP, _menu_path(item, sub)))
    time.sleep(1.2)


def set_remove_tool(on: bool) -> bool:
    """Pin the Remove (eraser) tool on or off so we know which panel shows."""
    if menu_flag("Remove") != on:
        menu_click("Remove")
    return menu_flag("Remove")


def single_panel_mode_on() -> str:
    """One Edit section open at a time, so Detail's contents land somewhere
    predictable instead of wherever the panel happened to be scrolled."""
    if not menu_flag("Single-Panel Mode", "Edit Panels"):
        menu_click("Single-Panel Mode", "Edit Panels")
        return "enabled"
    return "already on"


def activate():
    _osa(f'tell application "{APP}" to activate', timeout=15)
    time.sleep(1.2)


def grab() -> Image.Image:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        path = tf.name
    subprocess.run(["/usr/sbin/screencapture", "-x", "-t", "png", path],
                   capture_output=True, timeout=30)
    im = Image.open(path).convert("L").resize((LOG_W, LOG_H), Image.LANCZOS)
    Path(path).unlink(missing_ok=True)
    return im


TEMPLATES = Path(__file__).parent / "lr_templates"
LABEL_X0, LABEL_X1 = 1238, 1302   # where a control's label text begins


def _norm(vals):
    """Zero-mean, unit-variance — so a match survives the panel dimming that
    Lightroom applies while a progress window is up."""
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    sd = var ** 0.5 or 1.0
    return [(v - mean) / sd for v in vals]


def find_label(im: Image.Image, name: str, span, threshold: float = 0.55,
               label_x: int = None):
    """Find a control by its printed label rather than by counting checkbox
    borders, so the panel's scroll position stops mattering. Returns the
    label's centre y and the correlation score, or None."""
    path = TEMPLATES / f"{name}.png"
    if not path.exists():
        return None
    tpl = Image.open(path).convert("L")
    tw, th = tpl.size
    tvals = _norm([tpl.getpixel((x, y))
                   for y in range(0, th, 2) for x in range(0, tw, 2)])

    top, bottom = span
    best, best_y = -2.0, None
    for cy in range(top, bottom):
        y0 = cy - th // 2
        if y0 < 0 or y0 + th >= im.height:
            continue
        lx = LABEL_X0 if label_x is None else label_x
        vals = [im.getpixel((lx + x, y0 + y))
                for y in range(0, th, 2) for x in range(0, tw, 2)]
        if max(vals) - min(vals) < 20:        # flat region, no text here
            continue
        nv = _norm(vals)
        score = sum(a * b for a, b in zip(tvals, nv)) / len(nv)
        if score > best:
            best, best_y = score, cy
    if best_y is not None and best >= threshold:
        return best_y, best
    return None


def find_checkbox(im: Image.Image, span):
    """Centre of the first checkbox in `span`: a short solid run of bright
    pixels with a matching one 8-12 points below (the box's two borders)."""
    def run_len(ly):
        best = cur = 0
        for x in range(COL_X0, COL_X1):
            cur = cur + 1 if im.getpixel((x, ly)) > 110 else 0
            best = max(best, cur)
        return best

    top, bottom = span
    runs = {ly: run_len(ly) for ly in range(top, bottom)}
    for ly in range(top, bottom - BOX_MAX - 1):
        if not (8 <= runs[ly] <= 14) or runs.get(ly - 2, 0) > 14:
            continue
        for side in range(BOX_MIN, BOX_MAX + 1):
            if 8 <= runs.get(ly + side, 0) <= 14:
                return COL_CENTRE, ly + side // 2
    return None


def is_checked(im: Image.Image, centre) -> bool:
    """Ticked boxes are filled far brighter than the panel behind them. Compare
    against nearby background rather than a fixed number, because Lightroom
    dims the whole panel while a progress window is up — which used to make a
    ticked box read as unticked and report a false failure."""
    px, py = centre
    inner = [im.getpixel((x, y))
             for x in range(px - 3, px + 4) for y in range(py - 3, py + 4)]
    bg = [im.getpixel((x, y))
          for x in range(px - 24, px - 14) for y in range(py - 5, py + 6)]
    im_mean = sum(inner) / len(inner)
    bg_mean = max(sum(bg) / len(bg), 1.0)
    return im_mean > bg_mean + 45 or im_mean / bg_mean > 2.2


def click(pt):
    subprocess.run([CLICLICK, f"c:{pt[0]},{pt[1]}"], capture_output=True, timeout=20)


def lr_window_names() -> str:
    return _osa('tell application "System Events" to tell process "%s"\n'
                'set acc to ""\n'
                'repeat with w in every window\n'
                'try\n'
                'set acc to acc & (name of w) & "|"\n'
                'end try\n'
                'end repeat\n'
                'return acc\n'
                'end tell' % APP) or ""


def wait_window(word: str, seconds: int) -> bool:
    for _ in range(seconds):
        if word.lower() in lr_window_names().lower():
            return True
        time.sleep(1)
    return False


def wait_window_gone(word: str, seconds: int):
    for _ in range(seconds // 2):
        if word.lower() not in lr_window_names().lower():
            return
        time.sleep(2)


def guards() -> dict:
    return {f: menu_flag(f) for f in GUARD_FLAGS}


def guards_moved(before: dict):
    after = guards()
    return [f for f in before if before[f] != after.get(f)]


def run(kind: str, enable: bool):
    """kind is 'denoise' or 'dust'."""
    if _osa(f'tell application "System Events" to return '
            f'(exists process "{APP}")') != "true":
        print("Lightroom is not running.")
        sys.exit(1)

    activate()
    want_remove = (kind == "dust")
    if set_remove_tool(want_remove) != want_remove:
        panel = "Remove" if want_remove else "Edit"
        print(f"Couldn't switch Lightroom to the {panel} panel.")
        sys.exit(2)
    print(f"Panel: {'Remove (eraser)' if want_remove else 'Edit'}")

    span = DUST_SCAN if kind == "dust" else DETAIL_SCAN
    label = "Dust > Apply" if kind == "dust" else "Denoise"
    tpl_name = "apply" if kind == "dust" else "denoise"

    hdr_name, hdr_x = (("hdr_dust", 1236) if kind == "dust"
                       else ("hdr_detail", 1188))

    box = None
    # Staged recovery, because Lightroom can be left in any of several states:
    #   1. the control is already on screen
    #   2. the whole right panel is collapsed  -> click its tool icon
    #   3. the panel is open but the section is closed/scrolled away
    #      -> match the section header by name and click it
    for attempt in (1, 2, 3):
        shot = grab()
        hit = find_label(shot, tpl_name, span)
        if hit:
            cy, score = hit
            box = (COL_CENTRE, cy)
            print(f"Matched the {label} label (score {score:.2f})")
            break
        if attempt == 1:
            activate()
            click(ERASER_ICON if kind == "dust" else EDIT_ICON)
            time.sleep(1.8)
            # That icon toggles, so it may have switched tools as well as
            # opening the panel — re-assert the one we need via the menu.
            set_remove_tool(want_remove)
            print("Opened the side panel")
        elif attempt == 2:
            if kind == "denoise":
                print(f"Single-Panel Mode: {single_panel_mode_on()}")
            hdr = find_label(shot, hdr_name, (150, 950), 0.45, hdr_x)
            if not hdr:
                continue
            hy, hscore = hdr
            print(f"Opening its section (header matched at y={hy}, {hscore:.2f})")
            activate()
            click((hdr_x + 20, hy))
            time.sleep(2.0)
    if not box:
        shot = grab()
        box = find_checkbox(shot, span)      # last resort; every toggle is proved anyway
        if box:
            print("No label match; falling back to checkbox-shape search")
    if not box:
        where = ("Distraction Removal > Dust" if kind == "dust"
                 else "Edit > Detail")
        print(f"Couldn't find the {label} checkbox — expand {where} once, "
              f"then try again.")
        sys.exit(2)
    print(f"{label} checkbox at {box[0]},{box[1]}")

    if is_checked(grab(), box):
        print(f"{label} is already ON — nothing to do.")
        return
    if not enable:
        print(f"{label} is OFF.")
        sys.exit(3)

    before = guards()
    activate()
    click(box)

    if kind == "denoise" and wait_window("denoise", 12):
        print("Lightroom is applying Denoise…")
        wait_window_gone("denoise", 300)
        print("Denoise is now ON (confirmed by Lightroom's progress window).")
        return

    moved = guards_moved(before)
    if moved:
        click(box)                       # put back whatever we actually hit
        print(f"Hit the wrong control ({', '.join(moved)}) — undone. "
              f"Set {label} by hand.")
        sys.exit(4)

    if kind == "denoise":
        # Raw Details and Super Resolution sit right below Denoise and also
        # tick and show a progress window, so a ticked box is NOT proof we got
        # the right one. Only the "Applying Denoise" window is. Undo otherwise.
        click(box)
        print("That checkbox ticked, but Lightroom never showed the Denoise "
              "progress window — it was probably Raw Details or Super "
              "Resolution, so the change was undone. Scroll Edit > Detail to "
              "the top and try again, or tick Denoise by hand.")
        sys.exit(4)

    for _ in range(25):
        time.sleep(1)
        if is_checked(grab(), box):
            print(f"{label} is now ON (verified).")
            return

    click(box)
    print(f"Couldn't confirm {label}; change undone. Set it by hand.")
    sys.exit(4)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd in ("dust", "dust-status"):
        run("dust", cmd == "dust")
    else:
        run("denoise", cmd == "enable")


if __name__ == "__main__":
    main()
